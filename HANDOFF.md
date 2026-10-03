# DocMind · MCP 自动连接模块 接手 handoff

## 2026-10-03 Wave28：把两条"报出来不动手"的缺口修掉（`llm.py` / `api.py`，别人的模块但已入库）

用户点名处理 §6 缺口 14 与 16。两者原本都在别人手上（按纪律只报不改）；另一条 lane 做了
`e651c9c chore: 归集多lane 的在途改动到仓库` 把它们**入库**之后，两个文件的工作树是干净的，
"别动别人的在途改动"这条约束在**未提交**这个意义上不再适用。改动仍保持最小。

**① 缺口 14：`mock` provider 收不了一轮问答**（`llm.py` `_mock_chat`）

- 它「拿到观察 → 作答」那一支吐**裸文本**（既没 `Action:` 也没 `Final Answer:`），而
  `agent.py:3443` 的格式失守判定要求二者有其一 → 连拒两轮 → `final(status="error")`。
  **另两支（素材/知识库）都带 `Thought:` 与标记，只有这支漏了。**
- 后果不只是测试不好写：`mock` 是本仓**唯一零网络的模型路径**，也就是离线演示模式
  （没装 Ollama / 没有 key 时用户看到的那条路）——它同样跑不完一轮。
- 修法一行：加 `Thought: …` + `Final Answer: …` 前缀。验证：`/api/chat` 走 mock 打一轮，
  `final.status` 不再是 error、`chat_sse.verdict` 全绿、第一条工具调用仍在。

**② 缺口 16：`/api/config` 冷启动 6 秒**（`api.py` `_OLLAMA_CHECK_TTL`）

- **根因是环境，不是代码逻辑**：本机对**关闭的回环端口**做裸
  `socket.connect(("127.0.0.1", 11434))` 要 **2035ms**（正常应 <1ms；`urllib` 同路 2071ms）。
- **账算清了**（逐项计时）：`_live_capability` **4081ms**（首次构造 `LLMClient` 时它自己的
  实时探活）+ `check_ollama` **2030ms** + `_presets_payload` 11ms ≈ 6122ms（实测 6144ms）。
  先前只报了"另约 4 秒来源未定"，那 4 秒就是 `_live_capability`。
- **病根是 TTL 与探活同量级**：原值 2.0 秒，而探活本身约 2 秒（HTTP 超时 3 秒），
  `ts` 又在探活**结束**后才写 → 隔两三秒重开面板必然重付。原注释里作者已知
  "Windows 上一次被拒也要 ~2s"，但按业务新鲜度选了 2s，没意识到**缓存窗口小于操作耗时
  等于没有缓存**。
- 修法：TTL 2.0 → **30.0**，把这些数字写进注释。代价写明：启动 Ollama 后最多 30 秒才看到
  状态翻转；要即时结果仍可 `check_ollama(force=True)`（`GET /api/ollama/probe`）。
- **可重跑的同进程 A/B**（缓存从"已过期"起算，隔 3 秒再打）：`TTL=2.0 → 2047ms`
  ／`TTL=30.0 → 7ms`。首次冷启动**仍是 ~6.2 秒**（那是 `LLMClient` 首次构造的一次性探活，
  每进程一次，TTL 帮不到）。

- **产物与验证**：`tests/test_ollama_probe_cache.py` **4 项**（钉"TTL 必须大于一次探活的
  预算"这条不变量 + "窗口内第二次不再探活"这条**行为**判据）；`tests/test_chat_sse.py`
  新增 `MockProviderTests` **2 项**。回归 11 个文件 **244 passed / 41 subtests**。
- **边界**：`/api/config` 首次冷启动的 ~6.2 秒**没有降**（要降得让两次探活共用一次结果，
  跨 `api.py`+`llm.py`，未做）；30 秒的 TTL 是产品取舍，不是"正确值"。

## 2026-10-03 Vibecoding 能力补全 Wave27：`dev_apptest ask: 1`——问答那一档真的走完一轮（本会话，不占 R 槽位）

- **补的是 §6 缺口 15 的下半**：`/api/chat` 的真问答链路此前两处都停在 HTTP 层（§5.18 ②
  是**注入** LLM，§5.20 换成了生产 provider 路径但仍不打页面）。**页面那一层**
  （页面 → `/api/chat` → ReAct → 工具 → DOM）现在补上。
- **怎么点进那一轮**：输入区在子组件里（`AskApp.vue` 只渲染 `AskMessage`），所以不挖输入框，
  直接点**建议问题按钮**（`button.suggestion`，`@click="send(s)"`）。代价是问题文案不由我们定，
  所以凭据**不能依赖问题内容**。
- **凭据**：服务端用 §5.20 的 `llm_stub`（`provider=custom` + `LLM_BASE_URL` 指回环）当假模型，
  剧本第一轮调 `list_dir(.)` → 观察里带着**那一次运行独有的随机夹具文件名**（`probe_<6位>.py`）。
  回答里出现它就只能解释为整条链路真跑过。实测三次运行三个不同名字
  （`probe_23bb39.py` / `probe_8ff397.py` / `probe_6f6c8f.py`）。
- **三条被实测纠正的形状**：① `click` 多命中要 `nth`（问答页 3 个建议按钮）；
  ② 凭据**不能按位置挑**——`list_dir` 首行是表头、观察**末行常常是 harness 提示**，
  第一版先取首行再取末行各错一次，正解是**按动作找那类工具的输出行**；
  ③ `_drive_ask` 的 `open_page` 起初写在 `try` 外面，一抛 `finally` 就不执行、
  报告里只剩"凭据: None"。
- **桩要听懂纠正提示**：第一版不认识 `Evidence check:` / `Reflection:` / `Nudge:`，于是反复回
  `list_dir`，一轮问答跑 **6 轮**、最后靠"模型反复空转"的兜底停（页面渲染出兜底证据块而不是
  干净答复）。让桩在这三类提示出现时就着已有证据收尾后，一轮降到 **3 轮**，页面上的答复是
  `根据检索到的内容，[FILE] probe_6f6c8f.py —— 以上就是可用清单。`
- **产物与验证**：`tests/test_app_test.py` 新增 `AskTierTests` **9 项**（纯层）+
  `LivePageTests` 1 条现场用例；`battery_app_test` **58 → 66 条变异**
  （新增 8 条，含"空凭据也算通过"这种恒真形状）；`tests/test_llm_stub.py` **14 → 18 项**。
  八套确定性 **282 passed / 583 subtests**；十一块电池 `--no-live`
  **285/302 + 17 未验证 + 0 活下来 / 594 秒**；`app_test` 63/66 + 3 未验证。
- **边界**：证的是"页面把一轮问答跑通了"，**不**证模型理解问题、不证答案对不对；
  问题文案是页面自带的建议问题；`llm_stub` 是测试双，生产上没人给 `LLM_BASE_URL` 指回环地址。

## 2026-10-03 披露：`429edec` 把别人在途的 Wave21 文档一起提交了（同型事故第二次，**保留不改历史**）

- **事实**：`429edec` 里的 `docs/HANDOFF-vibecoding.md` 是 **1005 行**，而我本该提交的重建版是
  **851 行**——多出的 **154 行是另一条 lane 在途的 Wave21 文档**（`### 5.12`–`### 5.15`、
  `dev_deps` / `dev_probe` / `dev_handoff` / `dev_testrun coverage`）。
- **根因**：重建 blob 那一步我把**工作树里的**文件喂给了 `hash-object`（本该喂
  `.tmp/vibe26.md` 那份重建结果）。工作树当时 = 我的插入 + 别人 154 行在途 WIP，于是全被提交。
  这与 `b828d10` 是**同一个形状、方向相反**（那次是 `git commit -- <路径>` 绕过索引）。
- **按用户口径处理**：保留、不改写历史、如实披露（与 `b828d10` 同一口径）。代价是那 154 行
  现在停留在 HEAD 里，对方将来提交时不会冲突（他们的版本会包含同样的内容）。
- **复现**：`git show 429edec:docs/HANDOFF-vibecoding.md | wc -l` → 1005；
  `git show 429edec^:docs/HANDOFF-vibecoding.md | wc -l` → 803；
  差异里 `### 5.12` / `dev_deps` 等标记各出现 1~5 次（我重建的那份里一次都没有）。
- **两条流程修正（已进 `docs/HANDOFF-vibecoding.md` §9）**：
  ① 重建 blob 之前先确认"这份文件相对 HEAD 只差我的那几行"；**机械护栏**是提交前断言
  "差异里不含别人的标记"，命中就拒绝提交；
  ② **索引是共享的**——`git add <我的路径>` 不会让索引里只有我的路径。本轮实测：暂存区里
  多出别人 19 个文件（约 3600 行），而且**是持续的**（检查途中 `agent.py` 又冒出来）。
  本轮起改用**私有索引**（`GIT_INDEX_FILE` + `read-tree HEAD` + 只放自己的 blob + `git commit -F`）：
  既不碰共享索引，也没有竞态窗口。
- **代别人动过的账（已还原）**：为保护他们，我把那 18 个路径取消过暂存（**只动索引、没碰工作树**），
  事后已原样 `git add` 回去；抽查 `pricing.py` 仍是 35909 字节。本轮还把共享索引里我这 4 个
  文件刷新成与 HEAD 一致——否则另一条 lane 不 add 直接 `git commit` 会提交一棵**缺这 4 个文件**
  的树，等于回退我的提交。

## 2026-10-03 Vibecoding 能力补全 Wave26：`llm_stub`——问答链路在零网络下走到**生产 provider 那条路**（本会话，不占 R 槽位）

- **补的是 Wave24 §5.18 ② 漏掉的一层**：那一档的真回合是往 `api._agent_for` 的 Agent 上
  **注入**剧本 LLM，验的是 ReAct 循环、工具执行与 SSE 包装，但**不经过** `llm.py` 的
  provider 选择、`base_url` 解析与 OpenAI SDK 那条真实客户端路径。而 `dev_apptest` 把
  uvicorn 起成**子进程**，注入法在那里根本用不上。
- **做法与 Wave19a 的 `realtime_scripted` 同型**（只在显式选中时才生效的测试双）：
  `llm.py:567-571` 对 `provider == "custom"` 采纳 `LLM_BASE_URL`，所以把 `custom` 指到
  `agent_runtime/llm_stub.py` 起的回环假服务上，整条链路就是**产品里的正式 provider**
  ——`llm.py` / `api.py` / `config.py` **一个字没动**。
- **差一点走错的那一步**：进程内用 `os.environ` 设 `LLM_PROVIDER` **无效**——
  `config.LLM_PROVIDER` 是**导入时**读的（`.env` 里是 ollama），导入后再设太晚，现象是
  "去连 11434 报连接被拒"。进程内必须 `config.set_runtime(...)`；`env_for()` 是给
  **子进程**用的。
- **一条自己招的坑（已立测试盯着）**：`observation_snippet` 起初在**所有**消息里搜
  `Observation:`，而**系统提示里本身就有一大段解释 ReAct 格式的文字**（含这个词），
  于是桩把提示当成工具结果、第一轮就作答、**一个工具都不调**——判据报
  `at_least_one_tool_action` 红，看起来像 Agent 没调工具，其实是桩读错了消息。改窄到
  "只看最后一条"（`_mock_chat` 同款，`llm.py:1108`）。另有一条 `tool_name_in` 按**事件**
  形状写正则、而它要解析的是 **ReAct 正文**，永远返回空串。
- **验证**：`tests/test_llm_stub.py` **14 项**（纯 12 + 真起服务 2 + 真打 `/api/chat` 的
  provider 路径 2）。真回合实测：`check_ollama` **零次调用**、工具是 `search_assets`、
  `final` 原样引述目录里的 `· Pixel Platformer ［2d-sprites｜CC0］…`，
  `chat_sse.verdict` **14 条判据全绿**；八套确定性 **268 passed / 572 subtests**。
- **为什么这块没配变异电池**：本仓规矩是"**新守卫**必须配变异电池"，而 `llm_stub` 是
  **测试双**不是守卫——它喂的那个守卫（`chat_sse.verdict`）已有 20 条变异的
  `battery_chat_sse`。这是**有意的选择**，写明了免得后来人以为漏了。
- **仍缺（§6 缺口 15 的下半）**：**页面那一层**没做——问答页的输入区在子组件里
  （`AskApp.vue` 只渲染 `AskMessage`），要点出提问框 + 断言答案进 DOM 还得再挖一轮选择器。
  方向已写进 §6：先用 `button.suggestion`（`@click="send(s)"`）绕过输入框。
- **边界**：不证明模型理解问题、不证明厂商行为；"用假服务冒充 `custom`"是测试手段，
  生产上没人给 `LLM_BASE_URL` 指回环地址。

## 2026-10-03 Vibecoding 能力补全 Wave25：把"浏览器坏了"查成一条可以量出来的读数（本会话，不占 R 槽位）

- **起因是用户的一句反问**："怎么浏览器是坏的了"。我在 Wave24 交付后真浏览器那一档连红三次
  （3 红 / 4 绿，且红了没有信息量：`rows: 0`、`console_errors: []`、`failed_requests: []`），
  于是写下了"本机浏览器那一档现在是坏的"。**这个结论两个词都错**——它没坏，也不是纯环境
  问题。量完之后：**`open_page` 返回 ≠ 页面数据到位**。
- **时间线（实测）**：`open_page` 在 **1903 ms** 返回，文件树到 **5160 ms** 才出现——中间
  3.3 秒。`open_page` 的就绪判据是 `document.readyState === 'complete'`，而工作台把 UI
  门控在 `GET /api/config` 上。夹具在 `open_page` 返回后**立刻**读 `rows`，就是在跟这 3.3 秒
  赛跑：数据先到就绿、晚到就红——"有时绿有时红"就是这么来的。
  自己招的同类错：`_drive` 里**本来就有** `wait_for(".wb-shell", timeout=5.0)`，
  但它写在**读 facts 之后**，对 `rows` 那条判据完全没生效。
- **`/api/config` 冷启动 6.1 秒**（热了 15 ms）。拆开：`check_ollama(force=True)` 自己只要
  **2.0 秒**，**另约 4 秒来源未定**（`api.py` 属别人，本轮只报，记进 §6 缺口 16）。
- **`/api/health` 的 `ERR_ABORTED` 不是服务端慢**：health 单独 15 ms，**6 条并发慢
  `/api/config` 在飞时也只 24 ms**——我原先猜的"浏览器每主机 6 连接的饥饿"**被自己的测量
  推翻**。它是前端按设计取消的（`probeBackend()` 带 `AbortController` 4s + `catch { return false }`）。
- **三处修法**（都在本轮的文件里）：
  ① 读 facts 之前加两级等待（`.wb-shell` → `.ft-row`，各有截止期），并把
  `shell_waited_ms` / `data_waited_ms` / `data_ready` 上报；
  ② 把浏览器报的失败请求**分类**成"真失败"与"客户端自己取消"（`ERR_ABORTED`），判据只对
  前者变红，**两堆都进报告**（`failed_requests` / `cancelled_requests`）——静默扔掉是假干净，
  把取消算失败是假红；
  ③ 深链那档给"页面加载超时"加**记账式重试**（`open_page` 的就绪预算是硬编码的 6 秒，
  `page_action.py` 属别人不动），次数记进 `deep_retried_routes`，三次都超时就红并点名路由。
- **验证**：现场类 **7/7 通过**（141.29 s，含先前红的两条）；纯层与元测试
  **121 passed / 486 subtests**；七套确定性 **254 passed / 572 subtests**；十一块电池
  `--no-live` **277/294 + 17 未验证 + 0 活下来 / 533 秒**；`battery_app_test` 56 → **58 条变异**。
- **教训（已进 §9）**：三次同样的红只说明"这件事稳定地错"，**不说明错在哪**；
  "坏"是需要证据的结论，不是"红了几次"的推论——要定位就得**把中间量出来**。
- **边界**：`/api/config` 那 4 秒没有继续查；把客户端取消排除在 `no_failed_requests` 之外是
  一个**有意的收紧**，代价是"被客户端掐断的真实失败"也可能落进 `cancelled_requests`，
  所以它必须上报。

## 2026-10-03 Vibecoding 能力补全 Wave24：深链整页断言 + `/api/chat` 的 SSE 真问答链路（本会话，不占 R 槽位）

- **先纠正一个前提**：任务里写的 **`/api/ask` 不存在**（全仓零命中）。问答页真正打的端点是
  **`POST /api/chat`**（`api.py:2100`；`frontend/src/workbench/api/chat.ts:36`；
  `frontend/src/ask/AskApp.vue:508` 调的是 `aiApi.askGrounded`——那只是函数名）。
- **两块缺口都是"今天零自动覆盖"**：① `dev_apptest` 早就有 `page:` 参数，但**没有**任何断言
  证明"这一页真的渲染了自己"——三条页面路由各自独立、**没有 catch-all**（`api.py:3858`/`3863`/
  `3636`），谁把 `/trace` 接到 `index.html` 上，整页守卫照样 14 条全绿；② `/api/chat` 早有 6 条
  用例，但它们**全部**在 `Agent.run` 那一层打桩，也就是说经由这个端点，ReAct 主循环、
  provider 分派、工具执行**一条都没真跑过**。
- **深链（`dev_apptest deep_link: 1`）**：三条路由的挂载点**互不相交**
  （`.ask-shell` / `.wb-shell` / `#tablewrap`），所以"渲染了自己"与"没渲染别人"都可判定；
  每条路由开**新会话**（复用同一页面就是站内导航，证明不了冷启动）；两个 `.html` 别名
  （307）还要求**停在规范路径**上（实测跟完 307 之后 `href` 就是 `/workbench`）；反向对照是
  站内不存在的路径**必须不渲染任何一页**——没有它，一个 catch-all 会让上面全部照样绿。
- **`/api/chat`（新模块 `agent_runtime/chat_sse.py`）**：判据的核心是"**收到 final 不等于
  跑通一轮**"——`agent.py` 里有**六条不调模型也能产出一个 `final`** 的早退路径（容量兜底
  `offline` / 歧义追问 / 强制收尾 `degraded` / 格式失守 / 截断 / 异常 `status=error`），
  逐条挑出来；另两条协议层假象（`route` 与 `done` 是**无条件**发的）**不算证据**。
  凭据照 §5.10：`final` 必须原样带着某条工具观察里的一截。
- **为什么不用 `provider="mock"`（量出来的，不是偏好）**：mock 把 ReAct 循环与工具都跑起来了
  （实测 345 个 chunk、332 个 token、两次 action/observation），但**结构性地收不了尾**——
  `_mock_chat` 的「拿到观察 → 作答」分支吐的是**不带 `Final Answer:` 标记的裸文本**，而
  `agent.py:3443` 的格式失守判定连拒两轮后发 `final(status="error")`。**即 mock provider
  无法完成一次问答**，产品侧的离线演示模式同样跑不完。`llm.py` 正被别人改，**只报不改**
  （§6 缺口 14）。改走**可注入的剧本 LLM**，零共享文件改动：走 `agent_mod.LLMClient` 这个
  构造接缝（`agent.py:1703`），请求内任何新建的 Agent 都会拿到剧本对象。
- **零网络的三个前提**（缺一个就红）：① 压 `llm_provider`；② **还必须**压 `embedding_provider`
  ——分支条件是 `_prov == "ollama" or _emb == "ollama"`（`api.py:2168`），而 `.env` 把
  `EMBEDDING_PROVIDER` 设成了 ollama；③ 断言没有 `web_search`、观察里没有「未找到」。
  用例直接断言 `check_ollama` **零次调用**、剧本 LLM 真的被调用过。
- **本轮踩的坑全部记进 `docs/HANDOFF-vibecoding.md` §9**，最贵的两条：
  ① **电池接错线**——`battery_chat_sse` 的 `BIND` 写成 `"cs"`，而测试文件里的模块级名字是
  `chat_sse`，于是引擎的 `setattr` 只新建了一个没人用的属性、**纯层从头到尾看的是真模块**，
  11 条变异全部"活下来"，看着像守卫没牙；② **夹具形状抄错**——`action` 事件起初写成
  `{"tool": "..."}`，而真实载荷的工具名在 `text` 里，于是"没调联网工具"那条判据**恒真**。
  另有三条恒真/等价变异同型（反向对照传空 `foreign`、`rows.get() or {}` 把"没数据"当"没关系"、
  "只认 `data:`"在真实协议形状下区分不出来）。
- **产物与数量**：`agent_runtime/app_test.py` 深链那一档（`DEEP_LINK_ROUTES` / `_foreign_for` /
  `_console_matters` / `deep_link_facts` / `deep_link_checks` / `_drive_deep_link`）；
  `agent_runtime/chat_sse.py`（新模块，纯函数 `parse_events` / `verdict`）；
  `tests/test_app_test.py` 新增 `DeepLinkTests` 15 项 + 1 条现场用例（46 → **56 条变异**，
  `--no-live` 53/56 + 3 未验证）；`tests/test_chat_sse.py` **30 项**；
  `battery_chat_sse` **20 条变异 / 全抓 / 6 秒**；门禁 +1 题
  `chat-sse-refuses-a-final-that-never-ran-a-tool`（13 条断言），基线 26 → **27 题**。
- **验证**：七套 **252 passed / 570 subtests / 70.08s**（现场类单列）；十一块电池
  `--no-live` **275/292 抓到 + 17 条未验证 + 0 活下来 / 588 秒**；`battery_chat_sse` 20/20；
  `app_test` 53/56 + 3 未验证；整集门禁 27/27。
- **一条只有本机能看到的时序敏感（不是本轮代码问题）**：真浏览器那档本机**偶发**红，两种症状
  ——`/api/health` 被前端自己的 2.5s 探测超时掐成 `ERR_ABORTED`、以及 `页面加载超时`。
  **原有的** `LivePageTests::test_the_real_page_passes_every_line` 一样红，所以不是本轮的改动
  引起的。深链那条现场用例因此**刻意不进** `battery_app_test` 的 `LIVE` 名单——它一红会让整块
  电池的对照组作废，那比少一条凭据更糟；它仍在常规 pytest 里跑。
- **边界**：不证明厂商行为、不证明模型理解了问题；剧本 LLM 是**注入**的，不是生产选 provider
  那条路（`llm.py` 的 provider 选择是"`or` 链 + 字典查表"，没有 `register()` 钩子可挂）；
  深链证的是"每一页渲染了它自己"，不证页面内容对不对；`dev_apptest` 上的问答回合（页面那一层）
  仍没做（§6 缺口 15）。

## 2026-10-03 Vibecoding 能力补全 Wave23：处理三条"报出来但不是我的"红——两条动手，一条归还原主（本会话，不占 R 槽位）

- **接到的三件事与实际归属**：① 干净 HEAD 上 `tests/test_orchestrator.py` 12 红；
  ② `test_run` 电池 1 条活下来；③ 引擎不查锚点唯一性 + `run_isolated` 零测试。
  逐条查归属之后：**只有 ② 和 ③ 该我动手**。
- **① 是别人正在修的，不碰**：`agent.py:3931` 调 `parse_plan(plan, max_tasks=…)`，而 HEAD 的
  `orchestrator.parse_plan(raw, known_ids=None)` 不接受——HEAD 确实自相矛盾。但
  **`orchestrator.py` 在工作树里是 `M`**，对方版本已把签名改成 `parse_plan(raw, known_ids=None,
  max_tasks=None)`，另有 12 个 hunk 在改 `run_plan`/`dedup_tasks`/`apply_proposal`；他们的
  `agent.py` WIP 约 30 个 hunk 全是步数预算与提示词，**没有一处碰 `orchestrate`/`parse_plan`**。
  也就是说没人跟我抢，但我插手就会和他们在途的 12 个 hunk 撞车。HEAD 暂时红是过渡态。
  （我工作树里那条测试不是快速失败而是**空转 662 CPU-秒**，也是他们半成品在树里的现象。）
  用干净检出 + 把我的 `windows_sandbox.py` 换进去做过对照，两次都是 `12 failed, 45 passed`
  ——本轮零影响。**只报不改。**
- **② 是环境招出来的等价变异，补判据**：`agent_runtime/test_run.py` 的
  `if coverage and not coverage_available():` 只在本机没装 coverage 时走得到；Wave21 把
  `coverage>=7` 装进 `.venv` 之后这条路径恒不进入，变异成 `if False:` 自然等价。新增
  `CoverageDependencyGuardTests`（2 条）：直接把 `coverage_available` 打成 False，再断言
  **runner 一次都没被调用**（守卫在 `run()` 最前面短路）。该电池 **18/18**，0 活下来。
- **③ 引擎加锚点唯一性检查，先量影响面**：加之前扫了十块电池 256 条锚点——**只有 3 条
  不唯一，且全在 `battery_head_check`**（`head_check.py` 的 `render` 里有 12 空格缩进的同名块，
  4 空格那行是它的子串；`if missing:` 同理）。所以这条检查不会把任何别人的电池变红。
  现在 `anchors_valid()` 查 `source.count(old) == 1`，非唯一即报**问题**；3 条锚点改成带上下文。
  **有牙的证明**：用进程内替换模拟"检查不存在"，对应元测试当场变红（带检查 4 条全绿 / 关掉 1 条红）。
- **③ 顺带把 `run_isolated` 补上测试与电池**（此前它零测试）：新增跨平台 3 条
  （输出接得回来、退出码是自己的、两个流不合并）+ Windows-only 5 条（含一条**加入作业对象
  失败后那个已经起来的子进程必须被收掉**的漏进程判据，用"孩子本该写出、却永远没出现"的标记）。
  电池 **19 → 25 条变异**。
- **本轮最重要的发现：Wave22 那个提交在 CI 上是红的**。`.github/workflows/harness.yml` 在
  **ubuntu-latest** 上跑 `run_all.py --no-live`，而那 3 条 fail-closed 变异只被
  `@skipUnless(os.name == "nt")` 的用例抓着——ubuntu 上那些用例被 skip → 无人抓 → 记成
  **活下来** → 电池红 → CI 步骤失败。本机（Windows）跑是 19/19 全抓，**看不出来**。
  修法：把所有需要真 Windows 内核的用例归到一个 `WindowsOnlyLiveTests` 并声明成 `LIVE`
  （本电池的 `LIVE` 语义因此是"需要 Windows"而不是别处的"需要真浏览器"，在电池 docstring
  里写明，没改引擎契约）。现在 `--no-live` 是 **15/25 + 10 条本环境未验证 + 0 活下来**。
- **两条自己招的等价变异，都靠"把树加深一层 / 不依赖环境"解决**：超时回收那条我先写成
  "孩子直接写标记"，于是「杀直接子进程」与「终止作业对象」**单独去掉哪个都照样过**；
  改成"孩子拉一个孙进程去写标记"才可区分——而这正是作业对象存在的理由。
- **只报不改的新发现**：`run_isolated` 的 `Popen(..., text=True)` 没给 `encoding`，按本机
  区域编码（GBK）解码；子进程吐 UTF-8 中文时读线程抛 `UnicodeDecodeError`，那一路变成
  **`None`**（不是报错）。我的测试因此只用 ASCII 子进程输出。修它会改变所有适配器进程的
  行为，不在本轮范围，记进 §6 第 13 条。
- **验证**：六套 **204 passed / 1 skipped / 427 subtests / 94.52s**；`battery_windows_sandbox`
  带现场 **25/25**（235 秒）、`--no-live` 15/25 + 10 未验证 + 0 活下来；`battery_test_run`
  **18/18**；256 条锚点 0 条不唯一；引擎新检查的"关掉就红"证明见上。
- **边界**：引擎仍**没有"按平台分层"的概念**，所以给非 Windows 的 fallback 分支写变异会让
  两个平台里必有一个红——本轮**选择不写**那种变异并在电池里写明原因，这比写一条让 CI 红的
  变异诚实。`run_isolated` 的 fallback 分支只有回归保护（那 3 条跨平台用例），没有变异凭据。

## 2026-10-03 Vibecoding 能力补全 Wave22：作业对象的进程预算——把一条**确定性**的门禁红修掉（本会话，不占 R 槽位）

- **接手的第一个动作就撞上它**：`dev_headcheck gate: 1` 在 `e6078bb` 上稳定红三题
  （`diagnostics-find-real-errors`、`diagnostics-clean-file-is-clean`、
  `headcheck-catches-untracked-reference`）。上一轮把它们记为"负载下会抖"——**实测不是**：
  重跑两次一模一样，连失败名单都逐条相同。根因在 `agent_runtime/windows_sandbox.py:80`
  的 `limits.BasicLimitInformation.ActiveProcessLimit = 4`（裸字面量）。
- **机制**：门禁整集的子进程树是 `python -m dev_eval` → `python -m ruff` → `ruff.exe`
  （ruff 的 `__main__` 自己还会 `subprocess.run` 再开一层），而整集里别的题同时还留着并发的
  后端/浏览器/git 进程。并发预算花光后 `CreateProcess` 回 **`WinError 1816`（配额不足）**。
  三步实验：① 把完整原始输出打出来，看到的是 `subprocess.py:1554` 的 traceback；
  ② 6 个**串行**孩子全过（`Popen` 返回即前一个已退出）→ 卡的是**并发**不是深度；
  ③ 并发到第 4 个活进程就 1816。
- **它为什么特别难查，两层遮蔽**：① `diagnostics` 把这个 traceback **误报成**「ruff 退出码 1
  但输出没能解析（可能被截断）」，把人引向完全不同的方向；② 因为它在检出与工作树里**都**红，
  "比两边失败题名单差集"的归因会把它当公共噪声减掉——工具如实说"不是自洽问题"，同时报
  【未通过】且不给任何可行动信号。**这个门当时每天都在教人忽略自己。**
- **修法与依据**：`ACTIVE_PROCESS_LIMIT = 32`（具名常量，理由写进代码注释）。门禁整集
  **实测峰值 7**——作业对象 `QueryInformationJobObject` 采样，105 次采样 0 次失败，32 是 4 倍
  余量。单变量因果闭环：只把 `4` 临时改成 `64`，**同一条调用**从 22/25（3 红）变成 **25/25
  全绿**，真 `dev_headcheck gate: 1` 变成【通过】、干净检出 24/24；改完已精确还原、`git diff` 为空。
- **安全论证（不是"我觉得没关系"）**：回收整棵树的是 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，
  与进程数无关；**浏览器路径至今只设这一个 flag**（`visual_acceptance.py:242`），也就是说
  "只靠 kill-on-close"在本仓已经是交付过一次 99°C 事故的写法。真正的资源上限是
  `JOB_OBJECT_LIMIT_JOB_MEMORY`（1 GiB），`ACTIVE_PROCESS` 只是第二道刹车，所以新值**保留
  上限**而不是写成无界，并且有一条用例钉住"预算抬宽之后回收不能变松"。
- **顺带两处让限位可测**（此前该模块**零测试**）：把限位构造抽成 `job_limits()` 纯函数，
  并把模块级 `import ctypes.wintypes` 改成容错导入——它在非 Windows 上抛 `ValueError` 而非
  `ImportError`，而模块 docstring 早就写着"非 Windows 走 fallback，好让契约在 CI 上可测"，
  只是这条 import 把路堵死了。
- **产物与数量**：`windows_sandbox.py` **+46/-12**（唯一的行为变化就是 4 → 32）；
  `tests/test_windows_sandbox.py` **13 项**（9 纯层 + 2 fail-closed + 2 现场）；
  `battery_windows_sandbox` **19 条变异 / 全抓 / 33 秒**；门禁新增 1 题
  `sandbox-process-budget-survives-a-real-tree` **7 条断言**。
  **基线不动**：`compare_to_baseline` 把基线里没有的新题放进 `new_cases` 放行，只有"基线里
  已有且这轮翻转"的题才判回退——整集实测 **26/26**，`new_cases` 就是新增那一题。
- **本轮踩的四个坑，全部写进 `docs/HANDOFF-vibecoding.md` §9**：① 判据写成"孩子被回收了"
  而那个孩子**自己也会退出**，于是断言恒为真（假测试）；② `Popen` 返回 ≠ 进程起来了，所以
  "拉起 8 个"没证明 8 个**同时占着预算**；③ 探针用 `spawn_isolated` 起子进程却**从不读它的
  管道**，Windows 匿名管道约 4 KB 而门禁吐 27 KB → 子进程卡在 write 上，挂了 15 分钟；
  ④ 电池那条"活下来"是**锚点不唯一**——`run_isolated` 与 `spawn_isolated` 的
  `if ... AssignProcessToJobObject` 那一行字面完全相同，引擎 `replace(old, new, 1)` 把变异
  打进了没人调用的那个函数。现有元测试只查"锚点在不在"，查不出"是不是只出现一次"——
  已作为缺口 #11 记进 §6，**本轮没改引擎**（不在范围内）。
- **验证**：13 项新测试全绿（现场层 0.66s）；把上限临时打回 4，现场层以**一模一样的
  `WinError 1816`** 变红且耗时从 0.66s 涨到 120s（那 120s 正是在等永远不出现的标记）；
  电池 **19/19**；整集门禁 **26/26**；`dev_headcheck gate: 1` **【通过】**。
- **边界（不许越界宣称）**：证明的是"限位表摆得对、且门禁那棵树真的塞得进去"，**不**证明
  并发无关的稳定性；嵌套树的现场检查只在 Windows 上跑，CI（ubuntu）上那两条是 skip 而不是
  通过；纯层验的是**限位表与常量**，"进程有没有真被加进作业对象"只有现场层能抓；
  `run_isolated` 至今**零测试**（缺口 #12）。

## 2026-10-02 Vibecoding 能力补全 Wave21b：`dev_headcheck` 加第二档——在干净检出里真跑套件（本会话，不占 R 槽位）

> 编号说明：另一条 lane 也把他们的四个缺口（`dev_deps` / `dev_probe` / `dev_testrun coverage:` / `dev_handoff`）叫作 Wave21，尚未提交。为避免撞号，本会话这一档记作 **Wave21b**。

- **补什么**：Wave14 那条回路只证一件事——HEAD 能被 **import**。而 2026-10-02 我自己犯的第二次事故正好卡在它证不到的地方：hunk 暂存收进了别人那段的**尾巴**（`dev_eval` 里 6 条 `_check`），它们读的局部量还在别人未提交的上半段里 → import 全绿、`dev_headcheck` 全绿、**行为红**。当时是手工挂干净检出跑测试才抓到的；这一档把它变成工具：`gate: 1` 在那棵检出里真跑 dev_eval 门禁整集，`tests: 仓内相对路径` 真跑指定测试。
- **红了怎么归因（这条是设计核心）**：先重跑一次（门禁题里有计时敏感的，抖一下不算提交坏了；重跑绿了会**照样写在输出里**，不瞒），重跑仍红才拿**工作树**跑同一条当对照组，然后比**两边失败题名单的差集**而不是比布尔值。为什么：拿真事故 commit `f7ec14e` 实测，两棵树都因 `diagnostics` 两题在负载下抖而红——比布尔值就得出"两棵树都红，不是自洽问题"，把真正那一题埋了；差集一算，公共噪声自己减掉，报告点名 `only_in_checkout = [page-action-refuses-and-never-claims-success]`。
- **它第一天就被同一个 commit 复验了一次**：`check(ref="59b0fe5~1", gate=True)` → attempts=2、only_in_checkout 就是那一题、`likely = 只有检出里红的题：page-action-...`。这就是"验过的东西"和"能重跑的凭据"的差别。
- **两条被实测纠正的形状**：① `dev_eval --json` 一行 ~26 KB，而 `process_runner` 默认只留头 4000 字符 → JSON 被拦腰截断，"读不出结论"被当成"门禁没跑成"；这一档显式把窗口放到 120k，并有专门用例（结论能解析成字段本身就证明整行读全了）。② `run_bounded` 会把 `timeout` **静默削到 `MAX_TIMEOUT=300`**，所以我一开始写的 `suite_timeout: 600` 是谎报能力——上限改成钉在 `process_runner.MAX_TIMEOUT` 上（有元测试盯着这两个数字相等）。
- **跑批的落点必须在检出之外**：`DOCMIND_STATE_ROOT` 指到检出外的临时目录，否则套件写进检出、`worktree remove` 与"没留东西"那条判据就成了自证。
- **产物与数量**：`head_check.py` 新增 `safe_relative` / `gate_argv` / `pytest_argv` / `parse_gate` / `parse_pytest` / `attribute` / `run_suite` / `run_suites` 与 `check(gate=, tests=, suite_timeout=)`；`tests/test_head_check.py` 26 → **58 项**（+4 类：SuiteArgv / GateParsing / Attribution / CheckoutSuite / RealSuiteWiring / WholeRepoGate）；`battery_head_check` 14 → **44 条变异**；门禁题 `headcheck-catches-untracked-reference` 8 → **17 条断言**；三处注册齐（`tools.py` 函数 + `TOOLS` 描述 + `agent.py` 目录行）。
- **引擎也被这一轮改了**：事前新增"变异后的源码还得编译得过"检查——一条只替换半句的变异让整块电池崩在 `IndentationError` 上，而我第一版修法把它记成"抓到"（**白送一分**）；正确立场是：装不进的代码证明不了任何判据有牙，所以它是**这块电池的问题**，不是它的分数。`test_a_mutant_that_does_not_compile_is_a_problem_not_a_catch` 钉住这条。
- **两条"活下来"分开处理**：`row["failed_names"] = [] or sorted(...)` 是**等价变异**（恒等于原式）→ 换掉；另两条（`tests:` 那条路的失败题名单从没被读过）是**真缺口** → 补 `test_the_pytest_path_also_names_its_failed_tests`。
- **我自己又犯的一次同类错**（记进 `docs/HANDOFF-vibecoding.md` §9）：为了改假 runner 而**整段替换类体**，把夹在两个锚点之间的 3 条刚写的用例一起吃掉了——电池当场报 5 条活下来才暴露。教训：大块替换前先列出"这一段里有哪些用例"，替换后数一遍条数。
- **验证**：`tests/test_head_check.py + tests/test_mutation_batteries.py` **78 passed / 92 subtests**（离线那 57 项 22 秒）；门禁整集在该题上 **17/17**；`--only head_check --no-live` 电池 **44/44 抓到、0 活下来、529 秒**；`--only bat_guard` **35/35、4 秒**；端到端那条真检出用例单跑 **37 秒**通过。
- **边界（不许越界宣称）**：这一档验的是「**只有已提交内容时能不能跑成**」，不验行为对不对；需要未跟踪工具链（`frontend/node_modules`、`web/`）的套件在新检出里必然红——那种红是环境，报告靠差集把它减掉，而不是替它编一个解释；门禁整集在负载下有计时敏感的题（`diagnostics-*` 两题本会话实测在**两棵树里都抖过**），所以"重跑一次"是设计的一部分而不是遮羞布；`suite_timeout` 上限 300 秒是硬顶，慢机器上真可能超时，超时会明说而不是默认通过。


## 2026-10-02 Vibecoding 能力补全 Wave20：`bat_guard` 启动器引号静态门（本会话，不占 R 槽位）

- **为什么会做这个**：用户贴来一次运行失败——`[DocMind] 依赖准备失败：missing D:\WorkBuddy\rag-agent"\requirements.txt`。
  根因是 `run_desktop.bat` 里的 `--root "%~dp0"`：`%~dp0` **展开后以反斜杠结尾**，而外部程序按
  MSVCRT 规则解析 argv，`\"` 是「转义过的引号」→ 闭合引号消失 → 参数值尾部多一个字面 `"`。
  静态检查看不出来、语法检查看不出来，只有双击运行才炸。用户给的口径是「不动别人的文件，加一条静态门」。
- **前提是被量出来的，不是写在注释里的**：门禁题里有一条直接调 `subprocess.list2cmdline`
  （Python 自己拼 argv 的实现）——值 `D:\my dir\` 的正确写法是 `"D:\my dir\\"`，而手写 `"%~dp0"`
  发出去的是 `"D:\my dir\"`，**两串字节不同**，后者正是"转义引号"那个形状。零进程、零网络，ubuntu 也跑得动。
- **两条规则互为镜像**：`quoted_dir_expansion`（加了引号但展开后以 `\` 结尾）与
  `unquoted_dir_expansion`（目录展开裸着传给外部程序，路径有空格就裂成两个参数）。
  只写前一条等于建议「别加引号」——那是另一个 bug。正解只有 `"%~dp0."` 或 `"%ROOT:~0,-1%"`，
  判据里连这两个写法都必须绿，否则门会把人推向误修。
- **门的全部价值在"不越界"**：`cd /d "%~dp0"`、`if exist "%~dp0x.bat" goto y`、`set "R=%~dp0"`、
  `echo %~dp0`、`call "%~dp0lib.bat" --root "%~dp0"`（调批处理时参数交给 cmd，不交给 MSVCRT）
  都是**正确写法**。所以 `arguments_of()` 是公开函数，35 条变异里一大半是用**逐词结构断言**钉的：
  光看"报没报错"看不出实参认错了没有。
- **一跳追踪**：`set "ROOT=%~dp0"` 之后的 `--root "%ROOT%"` 是同一个坑，而且是把这行「修掉」的
  最自然写法——不追这一跳的门等于鼓励误修。两跳以上不追，写在模块文档的边界一节。
  反向也钉住：`set "B=%A%"` 排在 `set "A=%~dp0"` 之前时**不该报**（cmd 按执行顺序展开，那时 A 还是空的），
  这条有专门用例防止"顺手改成多遍扫"。
- **产物与数量**：`agent_runtime/bat_guard.py`（新模块，不占工具面）、`tests/test_bat_guard.py`
  **40 项 + 48 subtest**、`tests/mutation/battery_bat_guard.py` **35 条变异 / `--no-live` 全抓 / 4 秒**、
  门禁新增 1 题 `bat-guard-refuses-an-escaped-closing-quote`（**11 条断言**），
  基线按我的提交口径 `23 → 24 题`（工作树里还有别人那一题，本地刷全量是 25）。九块电池合起来 `--no-live`：**200/207 抓到 + 7 条本环境未验证 + 0 条活下来，229 秒**。
- **第一天就抓到两处**（都在别人**未提交**的文件里，本程序不代改、不代提交，改法各一个字符）：
  `run_desktop.bat:27` 与 `installer/uninstall.bat:9`（后者是 `powershell.exe -InstallDir "%~dp0"`，
  同一 bug 类的第二个实例，而且还没入库）。语料层因此分成两格并各自钉住：
  **当天晚些时候两处都由各自那条 lane 改成了 `"%~dp0."`**（现 `run_desktop.bat:35`、
  `installer/uninstall.bat:13`，注释里写的理由就是"闭合引号被转义"）：工作树层现在 0 findings，
  本程序全程没代改别人一行。留着的这一格防的是下一次有人再写回这个形状。
  `test_working_tree_launchers_are_clean`（这台机器上要真跑的那份，抓到过 2 处、现在为绿）与
  `test_head_committed_launchers_are_clean`（CI 看到的 HEAD 那份，绿）——
  分开是因为它们会因不同的事而红，合成一格就只能说清一半。
- **自己撞的三条**：① `Edit` 插入时又一次用「下一行的前缀」当锚点，把 `DEV_DATASET` 第一题的
  `steps` 行**吃掉**了——靠 `git diff --stat` 的插入/删除计数不对才发现（这是本会话第三次同型错，
  已写进 §9）；② 电池声明里没有 `LIVE = ()` 就被元测试 `AttributeError`（引擎读 `battery.LIVE`
  而不是 `getattr`），"没有现场层"也要显式声明；③ 基线文件的 `total` 是**一行计数**，
  没法按 lane 拆，我这次只能整文件带上并写明里面合并了别人那一题。
- **边界（不许越界宣称）**：只覆盖 `.bat`/`.cmd` 里「目录展开 × 引号」这一类 bug；
  `%VAR:~0,-1%` 剪的是不是真对、PowerShell 自己的引号规则、`start`/`pushd` 之类的边缘行为都**没有**；
  它也不证明启动器能跑起来（那是双击/`dev_apptest` 的事），只证明参数不会被引号吃掉。


## 2026-10-02 Vibecoding 能力补全 Wave19：`scripted` 离线实时适配器 + `dev_apptest stream` 真流式回合（本会话，不占 R 槽位）

- **补什么**：`dev_apptest`（Wave17b）明确写着"不听真网关"，而页面 + 麦克风 + `/api/vision/live-stream` 这条链路**以前只能靠人对着设备说话**（R12）。缺的不是模型，是"一个能自动被测的对面"。这一条补上：整条回路（真浏览器、真假麦克风、真 ws、真 DOM）第一次能被自动跑到，且**零网络、零 key、零模型调用**。
- **19a `agent_runtime/realtime_scripted.py`**：注册为 `scripted`，靠 `DOCMIND_REALTIME_PROVIDER=scripted` 选中——**走的就是生产选适配器那条路**（`realtime_provider.register` + 注册表），所以被测的是我们自己的接线而不是厂商。注册只往 `realtime_provider._BUILTIN_MODULES` 加了一行，**`api.py` 一个字没动**。事件载荷逐条对齐真实适配器 `realtime_omni.py`：`EVENT_TRANSCRIPT{text,role:user,final,language}` / `EVENT_TEXT_DELTA{text,role:assistant[,final]}` / `EVENT_AUDIO_DELTA{audio:裸 bytes,encoding:"pcm24",chars}` / `EVENT_DONE{response_id,reason}`，`"pcm24"` 的含义是 **24kHz 的 pcm16**（不是 24-bit），写成 24-bit 就是"播得快但看起来成功"那一类。
- **凭据设计（这条的核心）**：剧本回合的内容全部是**从收到的 PCM 现算**的量（片数 / 字节数 / 峰值 RMS）。于是"页面字幕里出现了这些数字"只能解释为麦克风真的流到了服务端。判据**不写死数字**（推几片取决于时钟与夹具循环位置），查的是自洽：`字节 == 片数 × 3200`、峰值落在 `(0.05, 1.0)`（喂静音凑不出这个带）、助手回包引用的字节数与转写是同一次实测（两个独立事件互相印证）、且至少**两轮**。
- **收轮规则故意照抄生产契约**：尾静音满 **10 片**才算说完（"停止说话 ≠ 停止推流"，Wave8 为这条付过真金白银），所以"喂 9 片"必须没有回合——夹具与守卫同一条线，断一处就红。
- **19b `dev_apptest stream: 1`**：`server_env(realtime=True)` 覆盖 ambient 的适配器选择 → 开页时带 `media_fixture.fake_media_flags()`（假麦克风 + 假摄像头）→ 点开发舱 → 打开摄像头（**这一步才建 ws**）→ 开麦 → 轮询字幕。现场实测：开麦后约 3 秒出第一轮，整档 18 秒。
- **三条被真页面纠正的形状**：① 假设备开关**必须在开页时**给，页面起来之后再传无效——漏传的现象不是红而是"没有字幕"，一开始把我去引向查上行（我第一版就漏了）；② 「开麦对话」按钮在 `liveVisionActive` 之前**根本不存在**（`AutonomousCockpit.vue:2636`），固定 sleep 1 秒就报"命中 0 个"，必须带截止期重试，而且开成之后文案会翻成「闭麦」；③ 轮次正则不能跨行：`.*` 配 DOTALL 会把第一条转写配到最后一条轮次，两轮只认出一轮。
- **默认路径没被动过**：`test_the_default_path_is_untouched_when_no_provider_is_configured` 钉住"没配 env 时 `hello.ok` 依旧报 `sampled-frames` + 原因"，`test_an_unknown_project_is_closed_before_any_provider_is_built` 钉住"项目不存在就 1008 关掉、一个 provider 都不建"。
- **产物与数量**：`agent_runtime/realtime_scripted.py`、`tests/test_realtime_scripted.py` **21 项**（15 纯 + 5 真 `TestClient` websocket + 注册表；**全部不需要浏览器，CI 跑得到**）、`app_test.py` 的 stream 档、`tests/test_app_test.py` **56 项**（50 纯 + 6 现场，现场含真流式那一档）、`tests/test_mutation_batteries.py` **20 项**、门禁整页那题 12 → **18 条检查**、`battery_app_test` 35 → **46 条变异**（新增 11 条流式判据；那条"开页时不带假设备"只有现场能抓，标了 `("live",)`）。
- **电池自己也被这条规则救了**：加完流式测试跑电池，**10 条流式变异全活下来**——不是守卫没牙，是电池的 `UNIT_CLASSES` 不知道有新类。于是元测试新增一条：**测试模块里每个有 `test_` 方法的 TestCase 必须落在 `UNIT_CLASSES`/`LIVE`/`LAYER_EXEMPT` 里**，不想当层的要写原因。另外把"变异数下限"从中央清单改成每块电池自己的 `MINIMUM_MUTATIONS`——共享工作树里有别的 lane 在加电池（今天多了 `page_action` / `job_logs` / `log_view` 三块），中央清单会变成互相挡路。
- **自查抓出的自己的错**：① 控制组红了一整轮才看清：我给 `server_env` 写的断言假设"环境里没有 `DOCMIND_REALTIME_PROVIDER`"，而这台机器 `.env` 就设着 `dashscope_omni` —— 改成"这个函数自己不该加、开了档必须覆盖 ambient"；② 一次脚本 `write` 写在 `ast.parse` 之前，把一个坏字符串真写进了 `dev_eval.py`（且把助手函数插到了另一题的作用域里）——教训：**先解析后落盘，插函数要确认宿主函数**；③ 三次 `Edit`/heredoc 把 `
` 弄成真实换行，直接制造 `unterminated string literal`。
- **验证**：`tests/test_app_test.py + test_realtime_scripted.py + test_vue_component.py + test_realtime_gateway_bridge.py` **195 passed / 75 subtests**（84 秒，含真浏览器现场）；相邻实时套件一起跑无回退；门禁整页题 18/18、vue 题 16/16。
- **边界（不许越界宣称）**：它证明**自家协议与页面接线**在真事件流下是通的，**不**证明厂商事件、打断语义、听感——那些仍归 R12 的人跑；`scripted` 需要显式选中，不在任何默认路径上；假麦克风只在有 Edge 的机器上验，CI 上那条现场用例是 skip 而不是通过；数字来自实测，但"这一轮说了什么"没有任何语义理解。



## 2026-10-02 Vibecoding 补充：`log_view` 长日志折叠与级别筛选 + `dev_job_logs` 的 fold/level（本会话 Side 分支，用户点名实现）

- **为什么不抬上限**：输出缓冲本来就是"头 4000 + 尾 4000，丢中段"（`process_runner.py:30-31`），抬上限只是把噪声灌进上下文。缺的是把判断下推到文本里。
- **签名而不是整行（量出来的）**：真起 uvicorn `--log-level info` 打 24 次同一接口，27 行日志**逐行去重还是 27 行**——每条 access 行都带临时客户端端口。按签名归一后 28 → 10 行、重复 21 次折成一段。样本入库 `tests/fixtures/uvicorn-access.log`，测试对着真样本断言，不写"看起来短了"。
- **三条被实测/电池删掉的东西**：① 引号内容归一——access 行引号里是请求路径，把 `"/api/settings"` 与 `"/api/models"` 折成一个签名是**把不同事件合并**，比折不动更糟；② IP 归一——`\d+` 已经吃掉它，规则冗余；③ 空白归一——举不出真实用例，电池里那条变异直接**活下来**，说明写的是猜测不是规则。另有一条等价变异（fold=False 仍分组）查出我留了两条产出完全相同的路径 → 合并。
- **级别筛选按块不按行**：`Traceback` 下面的正文行没有级别词，逐行筛会把最该看的筛掉。无级别行**继承**上一条识别到的级别；开头识别不到级别的行一律保留（保守方向）。实测门禁题 `traceback_body_follows_its_header` 钉这条。
- **视图不许冒充全文**：`middle_omitted_chars`（缓冲丢的中段）、`line_cap_hit`（超 2 万行没看）、`dropped_failure_marks`（被筛掉的段里带 4xx/5xx/失败 字样）单独计数并写在正文前。uvicorn 把 404 记成 INFO，`level: warn` 筛掉它合规，但那份视图永远不能被读成"没有错误"。计数自洽不变量：`lines_in - dropped_lines - folded_away + markers_added == lines_out`（`markers_added` 是合成行，不单列就对不上账）。
- **工具面保守**：`dev_job_logs` 加 `fold: true` / `level: warn|error`；不给这两个键时**原始增量文本一字不改**，只在确有重复/缺口时附 `view_hint`。视图模式整块重算不能续读：带 `offset` 会明说被忽略并把 `next_offset` 置空。
- **证据**：`tests/test_log_view.py` **26 项**；电池 `battery_log_view` **17/17**、`battery_job_logs` **6/6**；门禁 23 → **24 题**（`log-view-collapses-real-spam`，9 条判据全过）；`tests/test_page_action.py + test_log_view + test_native_tools + test_mcp_server_expose` **119 passed**。三处注册齐（实现 + TOOLS + `agent.py` 目录）。
- **一条不是我造成的红（如实报，未代改）**：`dev_eval.py` 里 `apptest-refuses-a-stale-build-as-current` 现在报 `NameError: name 'stream_fail' is not defined`——是主会话在写的 realtime/stream 档改到一半（`agent_runtime/app_test.py` 已加 `server_env(..., realtime=True)` 与 `tests/test_app_test.py::StreamPlumbingTests`，但门禁题里 `stream_fail` 未定义）。连带 `test_dev_eval` 三条红与 `app_test` 电池对照组红（电池对照组规则：**对照组红就整块不作数**，所以它报 0/46 而不是假装抓到）。这条归那条 lane，我不动。
- **边界**：只按可见文本与签名折叠，不做语义聚类；`level` 词表是固定保守表（认不出就当正文，不猜成 info）；视图是**给人看的摘要**，任何"日志里没有 X"的结论仍要用 `dev_apptest`/`contains` 那样的精确判据去坐实。


## 2026-10-02 Vibecoding 补充：`dev_page_action` 加 `mode: contains`，并修掉一个结构性死字段（本会话 Side 分支，用户点名实现）

- **为什么会做这个**：讨论「4000 字上限对 agent 是不是太少」时量出来的结论是——不该抬上限，该把判断下推到页面里。`read` 的其它模式都返回整段文本，于是「这句话在不在页面上」实际只回答了「它在前 4000 字里在不在」。
- **新形状**：`action: read` + `mode: contains` + `selector: <容器>` + `needle: <词>` → 回「可见命中几处 / 覆盖几个元素 / 扫了多少字符 / 最多 3 段周围样本」，返回量与页面大小无关。参数仍走 JSON 字面量，带引号括号的 needle 原样参与搜索（有专门用例验）。
- **顺手修掉一个永远为假的判据**：`INSPECT_JS` 先 `slice(0, 4000)`，Python 再判 `len(text) > MAX_READ_CHARS` —— 该条件**结构上不可能成立**，`truncated` 一直是 False，"读了一半"看起来像"读完了"。现在页面在截断之前算好 `full_length` 一起回传，判据改用它；`dev_apptest` 的 `page_text_read_wholly` 也因此从装饰变成活判据（实测本页正文只有 510 字，所以那条断言此前从未真的需要过）。
- **两处被真浏览器/电池纠正**：① 我以为 `innerText` 天然只搜可见文字——**不对**，元素不在渲染树里时它退回 `textContent`，真页面上 `#hid`（`display:none`）照样命中。现在用 `getClientRects().length === 0` 显式判可见，命中分 `hits`（可见）与 `hidden_hits`（不可见）两个数，样本只取可见处：「页面上有这句话」与「DOM 里有这句话」不再混为一谈。② 电池抓到样本上限有**两套数字**（JS 写死 3 + Python 再 `[:MAX_SAMPLES]`），删掉 JS 硬编码、改由 `spec.samples` 单一下发，与 `spec.limit` 同一套规矩；那两条变异因此从"活下来/等价"变成有牙。
- **诚实边界**：容器没匹配到时明说「这次搜索不构成任何证据」；没扫完（200k 字符 / 单元素 500 次）时复用 `truncated` 语义并明说「没找到不能当成没有」；只搜当前 DOM，不触发懒加载；iframe 与 Shadow DOM 依旧到不了。
- **证据**：`tests/test_page_action.py` 51 → **67 passed**（现场新增 2 条：`#hid` 可见性判定；4200 字之后的 needle 在 `mode: text` 拿不到而 `contains` 拿得到）；门禁题 `page-action-refuses-and-never-claims-success` 加 6 条离线判据（hidden/empty/上限下发/截断判据/needle 校验/容器围栏）；电池入库为 `tests/mutation/battery_page_action.py`，**18/18**（纯层+门禁+现场三层对照组），全套 `--no-live` 合计 **132/138 抓到、0 活下来**；相邻 `test_page_action / test_dev_eval / test_app_test / test_native_tools` 合计 **137 passed**。
- **顺手报一条别处的危险残留**（未代改属本程序文件，只改了这一行）：`docs/HANDOFF-vibecoding.md` §7 第 1 条仍写着 `git commit -- <只属于我的路径>` —— 正是 `b828d10` 误收的成因（pathspec 绕过索引）。已按 HANDOFF.md §9 的结论改为「hunk 暂存后只能裸 `git commit`」。


## 2026-10-02 Vibecoding 能力补全 Wave18：变异电池入库 + CI 可重跑（本会话，不占 R 槽位）

- **补什么**：Wave14–17 每一轮我都写了"变异电池 N/N 抓红"当凭据，可那五份脚本一直躺在 **gitignore 的 `.tmp/`** 里——也就是说所有"N/N 抓红"都是**不可重跑**的口头账，而且电池自身会烂：锚点被重构掉、测试类改名、现场类整类 skip，任何一种都只会让它"悄悄全绿"而没有任何东西变红。这一条把自己补上。
- **产物**：`tests/mutation/engine.py`（共用引擎：加载副本模块、**三层对照组**、逐条判定、把 `sys.modules` 换回真模块）、`tests/mutation/run_all.py`（CLI：`--no-live` / `--only` / `--list`）、五份**声明式**电池 `battery_{voice_loop,head_check,test_run,vue,app_test}.py`（变异清单原文搬过来，一条没改；循环从五份复制粘贴收敛成一份）、`tests/test_mutation_batteries.py` **18 项元测试**（0.24 秒）、CI 新步骤 `python tests/mutation/run_all.py --no-live -q`。
- **`--no-live` 的语义是这条设计的关键**：现场层是**兜底**而不是并列——引擎先跑纯层与门禁层，都沉默时才轮到真浏览器。标了 `("live",)` 的有 6 条（vue 4 + 整页 2），但离线实测只有整页那 **2** 条真的落到现场层（vue 那 4 条在 Wave17a 已经补出了纯层的牙，离线就能抓）。在没有 Edge 的环境里它们记成**「本环境未验证」**：既不算"抓住"也不算"活下来"，并且被打印出来。第一版我把同一条变异同时记进两个桶，CI 会自己被卡死；`test_a_live_mutant_is_never_counted_twice` 钉住这条。
- **元测试查什么**：每块电池的源码/测试类/现场用例名/门禁题号都还在不在、**每条锚点还对不对得上当前源码**（毫秒级，所以 CI 每次都真跑）、变异数下限（`MINIMUMS`，删变异必须显式改这里）、以及引擎自己的判定逻辑。
- **引擎自己也过了变异**（7 条手工变异，7/7 被元测试抓住，改完按 sha256 核对还原）：对照组不看"跑了 0 条"、现场 skip 被当成抓住、未验证又被记成活下来、总数对不上也算绿、锚点失效不再报、"等于没变异"不被拒、跑完不把真模块换回去。**测的过程又暴露了我自己的一个假测试**：那两条 live 判定用例的假层对【对照组】和【变异模块】给同一个返回值，于是引擎一进对照组就整块不作数，两条用例其实什么都没验——修成按模块分身之后才真的有牙（这已经是本会话第三次"假测试被自己的机制抓出"，前两次是 `setup_plan` 的死代码深拷贝与 `nth` 的写死默认）。
- **耗时（本机实测，`--no-live`）**：voice_loop 3s / test_run 10s / vue 14s / app_test 36s / head_check 182s，合计约 4 分钟——慢的那块是 `test_head_check` 里真建 `git worktree` 的用例，所以电池放在 sqlite 那一个 matrix 里跑，两个 matrix 不重复付费。
- **验证**：`run_all.py --no-live`（CI 那条）**118/120 抓到，2 条本环境未验证，0 条活下来，243 秒**；带浏览器的完整跑 `run_all.py --only vue,app_test` **72/72 全抓到，99 秒**——搬进仓库之后与 `.tmp` 时代的数字逐条对得上；`tests/test_mutation_batteries.py` **18 passed / 20 subtests / 0.24s**；引擎自己的 7 条手工变异 7/7 被抓。
- **边界（不许越界宣称）**：CI 那条只跑**纯层 + 门禁层**（现场层是兜底），整页那 2 条在 CI 上是「未验证」而不是"通过"——要真验它们仍然得在有 Edge 与前端的机器上 `python tests/mutation/run_all.py`（不带 `--no-live`）；电池证明的是"这些测试会红"，不是"代码是对的"；锚点元测试只保证电池没和源码脱钩，不保证变异集合**覆盖了**该模块的每个守卫（覆盖不到的地方仍然要靠 review 提出来）。



## 2026-10-02 Vibecoding 能力补全 Wave17：`dev_vue` 第二层（驱动共享状态）+ `dev_apptest`（真后端整页）（本会话，不占 R 槽位）

- **补什么**：Wave16 那条边界里说"依赖运行时全局的组件要另写 mount 包装"——先量了一下再动手：`frontend/src` 里 `provide(`/`inject(` **零命中**，本仓前端的组合式（`useWorkbench`）是**模块级单例**。于是 17a 的 mount 包装不是造假 provider，而是**入口与组件同源 import 同一份 + 挂载前调那个单例的方法**；17b 补 `dev_vue` 明确不证明的那一段——组件守卫的 props 是我喂的，整页要的是真 `/api` 往返。
- **17a 产物**：`vue_component.py` 加 `composable:` / `store_call:` / `setup: [{"call","args"}]` + `exported_names()`；`tests/test_vue_component.py` **79 项**（67 纯 + 12 现场）；变异电池 22 → **37/37**（并补了纯层/门禁/现场**三层对照组**——对照组红就整块电池不作数）。现场凭据：`store_call: useWorkbench` + `setup: openRootMenu({clientX:30,clientY:30})` 让 `ContextMenu.vue` 真的长出 `.cm-menu` 且 `count:.cm-item=2`，不驱动时同一份断言判红。
- **两处设计被实测纠正**：① `rendered` 从"判决"降级为"报出来的事实"——右键菜单**关着时什么都不渲染是正确状态**，判它红等于把正确行为当失败；要断言"渲染出了什么"必须写 `expect_present`。② **读状态不能靠点击**：`ContextMenu.vue` 监听 window `mousedown`，任何一次点击都会把它自己关掉，实测"点开了、读回来是空的"——改成页面自带 `MutationObserver` 盯 `#app`，`#state` 永远是当前状态，调用方只读不改。
- **17b 产物**：`agent_runtime/app_test.py`（工具 `dev_apptest`）、`tests/test_app_test.py` **43 项**（38 纯 + 5 现场）、`tools.py` 实现 + TOOLS 注册、`agent.py` 目录 1 行、`mcp_server.py` 归 WRITE_TOOLS、门禁 22 → **23/23**（基线已刷）、变异电池 **35/35**。实测：`uvicorn api:app` 冷启动就绪 **4.7s**，整轮含点击 **8.5s**，14 条断言全绿。
- **凭据设计的关键一条**：注册一个**只含本次随机命名夹具文件**（`probe_<6位>.py`）的临时项目，要求它出现在文件树文本里——随机名让"缓存/旧页面恰好有这句话"冒充不了。隔离判据是双的：临时状态根里真的落下了文件 **且** `GET /api/projects` 只列得出这一次的项目（漏传 `DOCMIND_STATE_ROOT` 时后面那条必红）。
- **旧构建不算现在的代码**：`bundle_built_after_latest_source` 要求 `web/**` 的 mtime 不早于 `frontend/src/**`；实测当场抓到过旧了 **4829 秒**的构建（同时必须把 `node_modules`/`.tmp`/`__pycache__` 从"源码"里排除，否则这条永远红）。本工具**不代跑** `npm run build`，只拒绝把旧的当新的。
- **三条被真页面纠正的形状**（先按直觉写，跑真页面才发现）：① `page_action` 的 `text:` 定位只在**可交互元素**里找，文件树那行是 `<span>`，实测命中 0 → 改成读 `.ft-body` 文本；② `locate` 的候选清单在 **64 个**就 `return`（`page_action.py:101`），所以"切页前后 `#app *` 都是 64"是**饱和**不是没变 → 内容换没换改用 `main` 正文指纹；③ `read_page` 有 4000 字上限，拿 html 长度比截断后两个值相等。另：`page_action` 禁 selector+text 同时给，「素材」在顶栏「素材区」徽标上也有，所以点击必须带 `scope: .ws-bar`。
- **现场三条红/绿**：全表绿；`page: /no-such-page-for-apptest` → 后端仍绿而 `shell_mounted`/`backend_data_reaches_page` 红；`api.py` 一导入就抛错 → `backend_started` 红并带出 `ImportError`。围栏：`page` 只接受站内裸路径、`http_json` 只允许回环主机 + `/api/projects`、`timeout` 20~600 秒，非法输入不起进程。
- **电池抓出的两次我自己的错**：① `setup_plan` 里我写了 `json.loads(json.dumps(args))` 的"防御性深拷贝"并配一条测试——入参本来就是 JSON 字符串，每次调用重新解析，别名不可能发生，那条变异**存活**证明测试是假的，代码与假测试一起删；② 两次 `Edit` 把 `old_string` 尾部换行处理错，一次把某条用例的函数体黏到 `def` 行上、一次直接吃掉了 `agent.py` 提示目录里的 `dev_testrun` 整行（从 HEAD 取回原行重插，`git diff --stat` 复核后现在只有 +1 行）。
- **验证**：`tests/test_app_test.py + tests/test_native_tools.py` **55 passed / 31 subtests**；`tests/test_vue_component.py` **79 passed / 44 subtests**；门禁 **23/23**；`dev_vue`/`dev_apptest` 都走文本协议真跑过。
- **边界（不许越界宣称）**：`dev_apptest` 不点真模型、不听真网关、不替组件级断言背书；它跑的是 `web/` 里的构建产物，所以**只能证明那份构建**与后端集成得起来；`frontend/src` 被别的 lane 改动时这条会红，那是"该重新构建了"而不是代码坏了。



## 2026-10-02 Vibecoding 能力补全 Wave16：`dev_vue` —— 把真实 `.vue` 组件挂进真浏览器验接线（本会话，不占 R 槽位）

- **补什么**：循环里最后一段没有守卫的接线。Wave8 的语音行为回路里那段胶水是我**照 `AutonomousCockpit.vue` 转写**的，不是从 `.vue` import 出来的——所以组件文件本身被改坏（props 改名、`emit('focus', id)` 变成 `emit('focus', label)`、`v-if="visible"` 写反）以前**没有任何测试会发现**。
- **零新依赖**（这是前提，不是附带优点）：编译器用仓库里已有的 `@vue/compiler-sfc`（vue-tsc 带的，3.5.42 与 `vue` 完全同版），打包用前端自己的 `esbuild@0.21.5` 的 **JS API**，点击/读状态用 Wave13 的 `dev_page_action`，静态服务用 Wave7 抽出来的 `serve_static`。vitest 那条路量化过不走（`vitest@2 + @vue/test-utils + jsdom` = 1 critical + 3 moderate + 1 high dev 公告，critical 是 Vitest UI 任意文件读；vitest 3 要 vite 6/7 而本树是 vite 5）。
- **产物**：`agent_runtime/vue_component.py`、`tests/test_vue_component.py` **62 项**（54 纯 + 8 真浏览器现场）、`tools.py` 的实现 + TOOLS 注册、`agent.py` 提示目录 1 行、`mcp_server.py` 归 WRITE_TOOLS、门禁加 1 题（21→22，基线已刷）。**顺手修掉 Wave13 的一个真缺陷**：`_target_spec` 把"没写 `nth`"和"`nth: 0`"都归成 0，于是多项命中时**无论怎么指定都点不到第一个**——`dev_vue` 在 `.wmd-item#0` 上撞出来，补了 `nth_given` 与两条回归用例（`tests/test_page_action.py` 49→51 项）。
- **被实测决定了的三件事**：① esbuild 只能走 JS API（CLI 没有插件位），且 `compileScript` 的输出**仍是 TypeScript**，返回的 `loader` 必须跟着 SFC 的 `lang` 走——写死 `'js'` 时 esbuild 在 `interface Member` 那行报 `Expected ";" but found "Member"`；② 页面要挂起来必须**从同一个 bundle 拿 `createApp`/`h`**（组件入口 `import Comp from '...'` + `export { createApp, h } from 'vue'` 一张图），否则是两个 vue 实例；③ 产物目录必须落在**前端树内**（`frontend/.tmp/vue-guard/`，命中 .gitignore 的 `.tmp/`）：入口里的裸说明符 `from 'vue'` 要靠从入口目录向上找 `node_modules`，放仓外直接 ESM loader 报错。
- **判定 fail-closed，8 条要求 + 6 条不变量**：`assertions_declared`（**只挂不断言＝未通过**，"挂上了"不是证据）、`emit_declared_by_component`（断言的 emit 名不在组件自己 `defineEmits` 的清单里＝未通过，否则会退化成"永远等不到"的假守卫）、`emit_recorded` / `emit_args_exact`（实参原样比较）、`state_read_back`（读状态那次点击抛错要**单独红**——不然 present/absent/count/text 四条一起"莫名没成立"）、`mounted`/`rendered`/`no_component_errors`/`no_vue_warnings`/`every_click_applied`/`artifacts_cleaned`。
- **两条守卫是修 bug 修出来的**：① `counts` 原本把整串 `.wmd-item=2` 当选择器传进页面，`querySelectorAll` 抛错把整份事实吞成空 dict，于是每条断言都只是"没成立"——现在 Python 侧 `count_plan` 拆成 `[选择器, 期望值]`，页面侧合并成**单条** `probe()`，并在测试里断言"失败分支既记错误又不谎报元素存在"；② 产物回收原来用 `shutil.rmtree(ignore_errors=True)`，浏览器刚关掉时 `app.mjs` 还被句柄占着，一次删不掉就永远没人知道——工作树里确实躺着这么一套孤儿目录，现在 `clean_target` 带重试并如实回报 `removed`。
- **变异电池 22/22 抓红**（`tests/mutation/battery_vue.py`（当时在 `.tmp/mut_vue.py`），只在副本上变异）。其中 **5 条一开始活下来**，原因同一个：那几处只有 `run_guard`/现场类才走到，纯层碰不到——于是把参数解析抽成 `parse_request()`、产物目录抽成 `workdir_for()`，把清理测试挪进纯层，并把"写死事件名""非法选择器谎报存在"两条改成断言**页面上的那一行**。还有一条是我的可用性判据把 `node_modules/@vue/compiler-sfc`（目录）用 `.is_file()` 去查，导致 8 条现场用例**静默跳过**而全量仍显示绿灯——判据改成复用模块自己的 `ESBUILD_API`/`SFC_API` 常量，并把跳过原因打进 `skipUnless` 文案。
- **反证不是想出来的**（现场类里四条真红）：`active:false` 时 `v-if` 必须让它什么都不渲染；断言 emit 实参是 `t9`（实际发 `t1`）必须只红 `emit_args_exact`；把组件**副本**的 `emit('focus', id)` 改成 `emit('focus', 'WRONG')` → 红；把 `v-if="visible"` 改成 `v-if="false"` → 红。副本一律写在 `frontend/.tmp/`（gitignore 内），**不原地改被监听的生产源码**。
- **验证**：`tests/test_vue_component.py` **62 passed / 0 skipped**；`tests/test_page_action.py` 51 项（含 2 条 nth 回归）；与 `test_native_tools` / `test_mcp_server_expose` / `test_dev_eval` / `test_vue_component` 一起 **140 passed / 60 subtests**；门禁 **22/22** 与基线持平；工具走文本协议真跑：真组件全绿并回 `emits=["focus"]`、`count:.wmd-item=2`、`text:1/2`，`props: not-json` / `../outside.vue` / `timeout: 1` / `counts: .a=b` 全部不起浏览器就拒绝。跑完按 `--user-data-dir` 数进程：24 个全是别人的（14 用户 Edge + 9 `codex-model-probe`），我的产物目录已空。
- **边界（不许越界宣称）**：只证明**这一个组件在这一组 props 下**的行为，不证明整页集成、不 proof 路由/父组件传参，也不听真网关；`expect_absent` 是"点完之后不在"，不是"永远不在"。
  **更正一条当时写错的边界**：那句"依赖 provide/inject 的组件需要另写 mount 包装"是**猜的**——Wave17a 实测 `frontend/src` 里 `provide(`/`inject(` **零命中**，本仓组合式是模块级单例，所以 mount 包装的真实形状是"同源 import + 挂载前调单例方法"（下一节）。


## 2026-10-02 Vibecoding 能力补全 Wave15：`dev_testrun` —— 跑测试→读失败→只重跑失败（本会话，不占 R 槽位）

- **补什么**：这一段以前只能 `run_command("pytest -q")` 然后把一整屏文本交回来自己读，实测有三个具体的漏法：① pytest **一个用例都没收集到时退出码是 5**（实测本仓 `pytest .tmp/empty_t` → exit 5），"没看到 FAILED"很容易被读成通过；② `-q` 的失败摘要（`FAILED <nodeid> - <原因>`）是文本里最容易漏读的几行；③ 全量 2569 项 / 5–15 分钟，改一处要重跑全部。
- **产物**：`agent_runtime/test_run.py`、`tests/test_test_run.py` **46 项**、`tools.py` 的实现 + TOOLS 注册、`agent.py` 提示目录 1 行、`mcp_server.py` 归入 WRITE_TOOLS（真的执行项目代码，但参数固定、不接受任意命令）、门禁加 1 题（题集 20→21，基线已刷）。**CI 不用改**：新题走已有的 `dev_eval` 门步骤。
- **判定 7 条，全部 fail-closed**：`runner_ran`（退出码必须落在 0~5 这组已知值里）、`finished`、`tests_collected`（计数 / 退出码 5 / 文本 "no tests ran" **三个信号各自都能单独判红**）、`summary_parsed`、`no_failures`、`counts_agree_with_exit`（摘要说 5 passed 而退出码是 1 → 判"报告不可信"而不是判通过）、`rerun_record_kept`。
- **`--lf` 在这台机器上不能用，所以记账自己做**：本工作树的 `.pytest_cache` 是 `Permission denied`（每条 pytest 都在刷 `PytestCacheWarning: could not create cache path .pytest_cache\v\cache`），pytest 自己的 lastfailed 存不下来。现在一律带 `-p no:cacheprovider`，失败 nodeid 记进项目状态目录的 `testrun/last-failed.json`，`only_failed: true` 用的是这份账；记账里非法 nodeid（选项样式、绝对路径、`..`、带换行）直接不进账。
- **两个是真跑出来才看见的设计错**（不是想出来的）：
  ① **`related` 一开始用 `--collect-only` 拿全仓 nodeid 做匹配**——实测本仓收集结果 **243,591 字符**，而有界缓冲只留 **8,022（`truncated=True`）**，大多数 nodeid 在截断里丢了，结果"什么都找不到"，还报成"这些源文件没有相干测试"。改成**按文件名在磁盘上选**（`tests/**/test_*.py`），代价与仓库大小无关，而且少一次全量收集。
  ② **`coverage` 没进 `_parse_keyed` 的 keys 列表**，于是 `coverage: true` 被静默丢掉、变成"跑一次全量 pytest"，300 秒后超时。现在补了 keys，并加了 `test_every_documented_key_reaches_the_module` / `test_coverage_flag_is_not_silently_dropped` 两条守卫——参数没落到模块，必须当场变红。
- **覆盖率不装假**：实测本仓 `.venv` 里 `coverage` 与 `pytest_cov` 都 `find_spec` → None，所以 `coverage: true` 直接回「未启用、缺依赖，那是【没测】不是 0%」，且**不起任何进程**。要不要加这个依赖留给你决定。
- **自查（变异电池 18/18 抓红**，`tests/mutation/battery_test_run.py`（当时在 `.tmp/mut_test_run.py`），只在副本上变异**）**：其中两条是电池跑出来才暴露的**测试自己没牙**——`tests_collected` 由三个信号合成，而我原来的用例把三个信号同时设成"坏"，于是删掉 `exit_code != 5` 或删掉 `no_tests_ran` 都照样全绿；改成**每个用例让另外两个信号说"绿"**，两条变异当场被抓住。另一条同类：`--tb=line` 的行数与摘要 FAILED 行数不等时硬配对，会把 A 的错误消息挂到 B 的 nodeid 上，补了不等就不配对的用例。
- **实测踩到的归因**：一次性把 9 个套件（含 3 个起真浏览器的）串起来跑，`test_page_action` 的 7 项现场报 `ConnectionResetError [WinError 10054]`；单跑同一文件 **49 passed / 19.4 秒**，同时按 `--user-data-dir` 归组数进程：23 个全是别人的（13 用户 Edge + 9 `codex-model-probe` + 1 用户 profile），我的一个没留 → 判为负载导致的 CDP 断连，不是代码回退。
- **验证**：`tests/test_test_run.py` **46 passed**；门禁 **21/21** 且与基线持平；真跑现场三例——`tests/test_head_check.py, tests/test_test_run.py` → pass 64 判通过；`tests/test_agent.py` → 2 fail 并逐条列出 nodeid + 原因（`AssertionError: 8 != 20` / `IndexError: list index out of range`），`only_failed: true` 精确重跑那 2 条（pass 0 / fail 2，mode=only_failed）；`related: agent_runtime/head_check.py` → 命中 `tests/test_head_check.py`，69 passed；非法输入（`paths: --collect-only`、`timeout: 3`、`timeout: 99999`）全部不起进程就拒绝。
- **边界（不许越界宣称）**：它只报【跑到了什么、哪里红了】，不修失败也不判断根因；`-q --tb=line` 拿到的是一行式现场，深层 traceback 仍要人 `run_command` 细看；`related` 靠文件名相干，命名不遵守 `test_*.py` 约定的仓它选不出来（会明说"没有相干测试文件"而不是退化成跑全量）；覆盖率未启用；不替代 CI 的全量 lane；记账是【每条命令一次覆盖写】，并发两条 `dev_testrun` 会互相顶掉。


## 2026-10-02 Vibecoding 能力补全 Wave14：`dev_headcheck` —— 把 HEAD 挂成干净检出验自洽（本会话，不占 R 槽位）

- **补什么**：一条**在工作树里永远看不见**的错。共享工作树里多个 AI 同时改同一批文件，只要发生 ① 漏 `git add`，或 ② `git commit -- <路径>` 绕过索引把别人的 WIP 一起提交掉（本仓 2026-10-01 真发生过，见上一节披露），结果都是"引用新模块的 import 进了 HEAD，新模块本身还在磁盘上未跟踪"。于是**在这个工作树里跑测试永远是绿的**，而 CI、别人的 `git worktree add`、任何 clone 都会在第一批 `import agent` 上红。缺的不是测试，是"只有已提交内容"这个视角。
- **产物**：`agent_runtime/head_check.py`（工具 `dev_headcheck`）、`tests/test_head_check.py` **26 项**、`tools.py` 的实现 + TOOLS 注册、`agent.py` 提示目录 1 行、`mcp_server.py` 归入 WRITE_TOOLS（会建/删临时检出并执行提交里的代码）、门禁加 1 题（题集 19→20，基线已刷）。
- **它做什么**：`rev-parse` 把 `ref: HEAD` **先钉成 sha**（HEAD 在共享工作树里会被别人推着走，验过的和报告里的必须是同一个提交）→ `git worktree add --detach` 到项目状态目录（仓外）→ 在**那棵树**里用 `sys.executable -c` 真 import `config,tools,agent`（cwd 决定 `sys.path[0]`，所以 import 的是提交里的代码，不是工作树的）→ 收回检出 + `worktree prune`。
- **失败要归因，两种红不是一回事**：缺的模块【在磁盘上但不在提交里】= 别人未提交的新文件被提交引用（本地能跑、单独检出必红）；【两边都不在】= 漏提交/改名/本该进 requirements。还会报**是谁引用的**：`agent.py:55（import agent_runtime.run_budget）`。
- **引用点的取法是被实测纠正过的**：第一版用一条"File 行后面跟一句 import"的正则整段搜，结果报的是 `<string>:3`——它匹配到的是 importlib 自己的帧，不是仓里那行。改成**逐帧解析**（traceback 帧头缩进 2 格、源码行缩进 4 格，body 只收 4 格那些行），再从最深的帧往前找与被缺模块匹配的那条。这条修正在测试里留着对应的两个用例。
- **自查**：变异电池 **14/14 全数抓红**（`tests/mutation/battery_head_check.py`（当时在 `.tmp/mut_head_check.py`），只在副本上变异）。其中两条是电池跑出来才暴露的**测试自己没牙**：① `ref` 形状校验——删掉校验后 8 个非法 ref 仍然全被拒（git 自己就报错），断言只看 `ok=False` 等于没测；改成断言**拒绝理由**必须是"形状不被接受"。② `--detach`——改成 `git worktree add <path> <sha>` 是等价变异（git 对非分支名本来就会 detach），抓不到；真正的风险是换成 `-b <name>` 会**在共享仓里凭空占一个分支名**，所以补了"分支表/HEAD/工作树/`worktree list` 四项都不许变"的用例，那条变异当场被抓住。
- **另有三条守卫是照着这次事故设计的**：空 `modules` 列表不许当通过（`all([])` 是 True）、子进程回 `{"ok": "truthy字符串"}` 不许当通过（只认 `is True`）、**没有本工具的归属标记就绝不 `worktree remove --force`**（绝不动别人的检出）。每条都有对应用例，且都在电池里被拆过。
- **验证**：`tests/test_head_check.py` **26 项**（含真 git 仓现场）；相邻套件 `test_head_check` + `test_voice_loop` + `test_media_fixture` + `test_page_action` + `test_frontend_node_tests` + `test_dev_eval` + `test_native_tools` 一起 **205 passed / 89 subtests**；门禁 **20/20** 且与基线持平；工具走文本协议真跑：默认 `【通过】HEAD=0dbaf51`、`ref: HEAD~1 / modules: config, tools` 正确回显钉住的 sha、`timeout: abc` 与 `timeout: 9999` 与 `ref: --help` 全部按形状拒绝。
- **这条工具第一次真跑就抓到了它自己诞生的原因**：在本仓 HEAD 上跑出 `agent → ModuleNotFoundError: agent_runtime.run_budget`，归因 `在提交里=False，在磁盘上=True`，引用点 `agent.py:55`——就是上一节披露的那次误收。随后另一条 lane 依披露提交了那 4 个模块（`0dbaf51`），现在同一条检查报【通过】。
- **边界（不许越界宣称）**：它验的是【能不能跑起来】，不验行为对不对；默认只 import 三个入口，**入口之外的深层 import 要靠 import 链真的走到才会暴露**（比如某个只在子命令里才 import 的模块它看不见）；不跑测试、不装依赖、不验 `requirements.txt` 完整性；临时检出落在项目状态目录，崩在半路仍可能留目录（有归属标记，下次不会误删）。


## 2026-10-02 披露：我的提交 `b828d10` 把别人在途的 WIP 一起提交了，并让 HEAD 变得 import 不了

- **事实**：`b828d10`（我那条语音行为回路的提交）在 `tools.py` 里带了 **272 行**、在 `agent.py` 里带了 **312 行**，其中属于我的只有 **39 行 + 2 行**；剩下的 ~457 行是另一条 lane 未提交的 step-budget / capacity / web-research WIP，被挂在**我的提交信息**下入库了。
- **根因（这次是流程错，不是判断错）**：我**先**按 hunk 只暂存了自己的改动，并用 `git diff --cached --stat` 核对过行数就是 39/2 —— 暂存这一步是对的。错在最后一条命令用了 `git commit -- <我的 8 个路径>`：**pathspec 形式的 commit 会绕过索引**，它从 HEAD + 这些路径的**工作树内容**临时建一个索引，于是我把刚核对过的 hunk 子集整个丢掉了。`git show --stat` 一跑就露馅了。
- **可复核的后果（实测，不是推理）**：把 HEAD 挂成干净 worktree（`git worktree add --detach`）后 `import agent` 直接失败——
  `agent.py:55 from agent_runtime.run_budget import RunBudget → ModuleNotFoundError: No module named 'agent_runtime.run_budget'`，
  因为 `agent_runtime/run_budget.py` 与 `agent_runtime/capacity.py` **至今还是未跟踪文件**，被扫进 HEAD 的只有【引用它们的那几行】。
  逐条比过：`git show 59fddb5:agent.py`、`git show 40ae86d:agent.py` 里这条 import 出现 **0 次**，`b828d10` 里 **1 次** —— 是我的提交引入的。
  任何从干净检出跑起来的消费方（CI、别人 `git worktree add`、`git clone`）都会在第一批 `import agent` 的测试上红，包括我自己的 `tests/test_voice_loop.py::ToolSurfaceTests`。
- **当前工作树没事**：那几个模块还在磁盘上（未跟踪），所以**在这个工作树里**跑一切正常。坏的只是"单独检出 HEAD"这一种视图。
- **归属**：未跟踪的 `agent_runtime/run_budget.py` / `capacity.py` / `tests/test_run_budget.py` / `tests/test_agent_capacity.py`，加上 `tools.py` 里 `web_search`/`_custom_fetch` 那些 hunk，属于**另一条 lane**（就是之前那 12 条红的 owner）。我没有代改、也没有代提交他们的模块。
- **用户决定**：保留 `b828d10`、如实披露，**不改历史**。若要干净收尾，一条命令就够（需要 owner 与本会话同意，因为是历史改写、且尚未 push、`b828d10` 在 reflog 里随时能回来）：
  `git reset --mixed HEAD~1` → 重跑 `.tmp/stage_hunks.py`（tools.py 5 段 / agent.py 1 段）→ `git add` 我独占的 6 个文件 → **不带 pathspec** 的 `git commit`。
- **给所有 lane 的规矩（我这次没守住的那条）**：hunk 级暂存之后**必须用裸 `git commit`**（提交索引），`git commit -- <路径>` 是"按工作树提交这些文件"的意思，不是作用域工具。提交完立刻 `git show --stat` 看共享文件的行数有没有超过自己的 hunk；再多做一步：`git worktree add --detach <tmp> HEAD` 里 `import agent` 一次，确认 HEAD 自洽。


### R14 独立复核（本会话，只读复现，未代改/代提交任何 lane 的文件）

- **不是转述，是自己挂干净 worktree 复现的**：`git worktree add --detach /d/Temp/dm-headcheck HEAD` → 该检出里 `agent_runtime/run_budget.py` **不存在**，`import agent` 报 `ModuleNotFoundError: No module named 'agent_runtime.run_budget'`。作者结论成立。（`agent.py:55` 是**模块级**裸 import，不是函数内的惰性 import，所以拦不住；`:75`/`:159` 的 `capacity` 才是函数内的。）探针 worktree 已 `git worktree remove --force` + `prune`，`git worktree list` 只剩主树。
- **补一条作者没写、但会直接挡 push 的后果**：`harness.yml` 在干净 runner（`actions/checkout@v4`）上跑的**每一个**门槛步骤都要 import 到 agent —— `compileall agent_runtime`、`workflow_eval`、`dev_eval`、`retrieval_eval`、`unittest` 主步骤、pytest lane、`test_game_workflow_e2e` 那步。所以「HEAD 不自洽」不只是别人 `git worktree add` 的冷门视图：**这 9 个提交一 push，CI 会整片红**。顺序上应当先让 HEAD 自洽，再 push。
- **补一条时间事实**：那 4 个未跟踪文件最后修改停在 **10-01 04:24–04:30**（复核时约 21 小时无改动），所以「等 owner 自己提交」并不是「马上就好」。
- **同一时点的工作树实测（`unittest discover -s tests`）**：**2322 项 → 9 failures + 4 errors + 10 skipped**。红点全部落在未提交的在途改动上，没有一项来自已提交内容：`test_workflow_step_budget.py`（`M`）6 项、`tests.test_agent` 1F+1E、`test_agent_capacity.py`（`??`）1E、`test_game_workflow` 2E、`test_orchestrator` 1F。
- **其中至少一条是「非确定性」而不是「还没做完」**：`RunChildStepCapTests.test_request_above_hard_cap_is_clamped` 期望 12，实测值在两次运行间从 **10 变成 11**。同一份代码两次给出不同数字 ⇒ 这些断言依赖全局/顺序状态，不能只当 WIP 未收尾。
- **最小且不改历史的收尾路径（供决策，本轮未执行）**：把那 4 个未跟踪文件纳入版本库即可让 HEAD 自洽 —— **纯新增、无内容改动、`git reset` 可回退**。但这等于把别人 lane 的在途文件挂到我的提交下，**正是 `b828d10` 的错法**，所以需要 owner / 用户明确授权；授权前我不代提交。
- **用户决定与执行（本会话）**：授权「我把这 4 个文件入库」，且顺序为**先修 HEAD 再 push**。已执行：只 `git add` 这 4 个明确路径 → **裸 `git commit`（不带 pathspec）** → 再用**新的**干净 worktree 复验 `import agent` 与 `compileall`，两个视图都通。性质是**修复不是认领**：文件内容一字未改，只是把 HEAD 已经在 import 的东西纳入版本库；`git reset` 可回退。另复核过 `capacity.py` 的两条依赖（`gpu_coordinator` 已跟踪且为函数内 `try`、`config` 已跟踪），**没有**引入新的未跟踪依赖；同时确认 `dependency_bootstrap.py` / `head_check.py` 这两个未跟踪模块**未被已提交代码引用**，故不在本次修复范围。
- **R14 补充（推送后实测，供其他 lane 别重复诊断）**：11 个提交已推成功（逐对象 `MATCH`，远端 `main` = `0dbaf51` = 本地 HEAD）。CI run `36903320740`：`postgres tests` **success**；`sqlite tests` 仅 `Run pytest-style lane` 一步红，而**需要 import agent 的门槛步骤（`Compile runtime` / `workflow_eval` / `dev_eval` / `retrieval_eval`）全部通过** ⇒ `b828d10` 引入的「缺模块导致干净检出 import 失败」已消除。剩下的 `tests/test_mcp_discovery.py::test_github_routing_offline_injected`（`1 failed, 308 passed`）是**既有**红，不是本轮引入：该文件未被本地改动、也不在本次 11 个提交的 diff 里，本地 Windows 上单跑 **1 passed**，且上一次 CI（`36713588669`，09-30）**同一测试同一失败**。`ok` 由 `bool(candidates)` 得出 ⇒ Linux runner 上候选为空；模块内无平台分支，根因未定位（本地无法复现 Linux）。
- **R14 追加：那条既有红的根因已定位（本机逐字复现，未代改该测试）**。`tests/test_mcp_discovery.py:55` 的启动器解析桩写成 `os.path.join("C:\\tools", launcher)`，而**这个字符串只在 ntpath 下是绝对路径**：POSIX 上 `join` 产出 `'C:\\tools/npx'`，`posixpath.isabs` = **False**。于是 `mcp_candidates.py:135` 的 `abs_ok = (resolved != launcher) and os.path.isabs(resolved)` 为 False → `command_unresolved=True` → `mcp_validation.py:146` 的 R3 直接 error「启动器未在环境中解析到绝对路径」→ 候选被丢 → `discover_from_need` 返回 `ok = bool(candidates) = False` → CI `assert res["ok"] is True` 红。**复现方法**：把 `mcp_candidates.os.path` 换成 `posixpath`（模拟 Linux 的 `os.path`），同一份注入下 `ok=False` 且错误文案与 CI 一致。**候选修法（已验证两平台都绿，未落地）**：桩改成平台中立写法 `lambda launcher: os.path.join(os.path.dirname(sys.executable), launcher)` —— Windows 得 `D:\...\Scripts\npx`、POSIX 得 `/usr/bin/npx`，都是绝对路径，且仍满足断言要求的「command 含 npx」。该测试文件**无在途改动**（最后变更在 `9136eed`），修法是测试侧一行、不碰被测逻辑；等 owner / 用户点头再落地。

## 2026-10-01 Vibecoding 能力补全 Wave8（后半·选项3）：语音接线的浏览器行为回路 `dev_media action: voice`（本会话，不占 R 槽位）

- **补什么**：Wave7 的探针页面**刻意不接能量门**（逐帧无条件推流），所以它只能证明"采集→WS→R0 线上"，证不了 9/29 那次真机事故的那半条契约——**静音期不发流、说完再推满 1 秒、推完真的停下来**。这一条原来落在"要装 vitest 才能做"的账上；装过一次并量化了代价（`vitest@2 + @vue/test-utils + jsdom` = **1 critical + 3 moderate + 1 high** dev 公告，critical 是 Vitest UI server 任意文件读；vitest 3 链要 vite 6/7 而本树是 vite 5），**已回退**，按用户选的选项3 改成用真浏览器验行为。
- **产物**：`agent_runtime/voice_loop.py`（工具入口 `dev_media(action: voice)`）、`tests/test_voice_loop.py` **42 项**（含 4 项真浏览器现场）、门禁加 1 题（题集 18→19，基线已刷）、`tools.py`/`agent.py` 相应两行。零新依赖：编译用的是**前端自带的 esbuild**（`node_modules/esbuild/bin/esbuild --bundle`），页面 import 的是编译后的**生产模块本体**（`liveAudioControl.ts` / `realtimeProtocol.ts`），不是我抄的逻辑副本。
- **胶水是逐段转写的**：`sendLiveAudioChunk` / `sendNativeAudioReady` / `handleLiveAudioFrame` / `startMic` / `stopMic` 对着 `AutonomousCockpit.vue` 抄了调用、顺序、tail 期零值替换、`pump.pending()`、`bufferedAmount > 500000` 背压丢弃、`hello.ok` 之后发标记片、`stopLiveMic` 的 `gate.reset(); pump.reset()`。`GLUE_CALLS` 十个生产符号在 **import 之后的函数体里**必须都能找到——只看"名字出现过"是废话（import 行本身就写着它们）。
- **判定是 14 条，每条都必须能红**（这是这一波的真正产物；`passed = all(checks)`）：静音期漏流、尾长不足 1 秒、**尾长超上限=无条件泵**、**尾后有没有真的停**、序号单调、片形 3200 字节、协议违规、抢话发 cancel、收口事件**页面也收到**、单连接、console/失败请求、mic 起来、hello.ok 回来。`render()` 打印的是**本次实际用的阈值**（`result["thresholds"]`），不是常量。
- **独立审计（另一个 agent 读 diff）报了 11 条，逐条核实后 8 条成立并改掉**，其中三条是"看起来在守其实没守"：① **截断在静音尾中间的、完全没接门限的无条件推流页面 → 全项通过**（旧代码把"撞列表末尾"当成"停下来了"；现在 `tail_stats` 返回三态 `yes/no/not_observed`，必须**尾后又继续观察够 `RESUME_GAP_SECONDS`** 才算停，`run_voice_loop` 因此在最后一个包之后仍多观察 0.7 秒）；② `sequence_monotonic` 对**单包**和**非整数 sequence** 返回 True；③ `connections` 从没进判定（开两条连接=重复起流）；另 ④ console 错误与失败请求没进 `no_page_errors`，⑤ 收口只信探针自己发的 `model.delta final`、不信页面收到没，⑥ `build_bundle` 不删旧产物、返回 0 但产物是空文件也算成功，⑦ `render()` 打印常量阈值。
- **变异电池 16/16 全部抓红**（脚本 `tests/mutation/battery_voice_loop.py`（当时在 `.tmp/mut_voice_loop.py`），只在副本上变异，不碰工作树）：尾内间隔当尾后、截断当停止、单包算单调、字符串序号蒙混、多连接算一条、探针说收口就算收口、console/失败请求不进判定、上限不判、静音漏流不判、GLUE_CALLS 退回整页找名字、render 打印常量、开麦时归零序号、旧产物不删、空产物算成功、点击文案写成元组。**其中"开麦时归零序号"这一条是真 bug 而不是假想**：现场跑 `sequence_monotonic` 当场变红——生产是在**新建 socket** 时归零 `liveAudioSequence`（`AutonomousCockpit.vue:1433`），我在 `startMic` 里归零，于是 `hello.ok` 后的 audio-ready 标记片和第一片语音撞了同一个序号。现已把序号归零放回 `connect()`，并加了一条无浏览器就能守的文案/序号测试（`click_labels()`）。
- **反证的形状变了，实测才知道**：纯静音脚本**不再是 0 个包**——生产在 `hello.ok` 之后必发 1 片 100ms 静音标记，所以正确断言是"≤1 片、且没有响亮片、且不收口"。相应把 `allowed_leading` 定成与 `barge_in` 无关的 1。
- **验证**：`tests/test_voice_loop.py` **39 passed**；与相邻套件（`test_media_fixture` / `test_page_action` / `test_frontend_node_tests`）一起 **152 passed / 70 subtests**，现场 4 项真跑（真点击、真 `getUserMedia`、真 esbuild）；门禁 **19/19** 且与基线持平无回退；跑完现场检查 msedge：15 个进程全是别人的（8 个属于 `codex-model-probe`、5 个是用户自己的 Edge），我的 1 个残留 profile 目录已按"无存活属主"确认后删除。
- **边界（不许越界宣称）**：`.vue` 的接线本身仍然是**转写**的、不是 import 的，组件级守卫这半没做（按用户选项3 的决定不做 vitest）；真网关（`api.py` + DashScope）与听感仍归 R12 人跑；`quiet` 段经浏览器 AEC/NS 后能量会被抬到能开门，所以"低于门限"只对生成的文件成立，不能拿它在回路上断言"绝不开门"。


## 2026-10-01 Vibecoding 能力补全 Wave8（前半）：前端行为测试接进守卫（本会话，不占 R 槽位）

- **补什么**：不是"缺测试"，是**测试写了却从来没跑过**。`frontend/tests/` 里躺着 5 个 `node:test` 文件、12 项断言，跑的是 `src/workbench/` 的真逻辑（异常提醒的确认链、选区映射、编码噪声是否反复叫视觉模型、视觉动作环的恢复与项目隔离、语音与帧的配对），但 `frontend/package.json` 没有 test 脚本、CI 里没有对应步骤——全仓引用它们的只有 HANDOFF 和 memory。所以这 12 项对回归的贡献一直是 **0**。
- **产物**：`frontend/package.json` 加 `test:node` 脚本、`.github/workflows/harness.yml` 加「Run frontend behaviour tests (node:test)」步骤（sqlite job，`npm ci` 之后，零额外安装）、`tests/test_frontend_node_tests.py` **5 项**守卫。没有新增任何依赖，lockfile 一行没动。
- **这一波真正的价值在那条 Python 守卫，不在 CI 步骤**。测出来的陷阱：`node --test` **匹配到 0 个文件时退出码仍是 0、`# tests 0`**，`test.skip` 同样不影响退出码——所以任何「跑一下看退出码」的接法都会把「用例被删/被改名/被 skip」显示成全绿。守卫的判定核心因此是**计数比对**：先从源码正则数出声明了多少个 `test(`/`it(`，再要求 node 实跑的 `# tests` 等于它，且 `skipped == 0`、`fail == 0`。声明数是算出来的，加用例不需要改守卫，删用例会立刻红。
- **变异测试证明它有牙**（三连，脚本 `.tmp/w8_mutation.py`）：① 基线绿 → ② 往生产用例文件注入一个 `test.skip(...)`，守卫变红 → ③ 同样的注入、只把守卫里那两行计数断言删掉，又变绿。三条同时成立才说明红来自计数而不是巧合。
- **另外两条顺手钉住的**：① 用例必须真的 import 生产模块（`../src/workbench/<name>.ts` 存在性逐个断言），防止以后有人把测试改成测副本；② `test.skip` 之类静默失效的场景外，还把"glob 空匹配对 node 是绿的"这个事实本身写成一条断言（`returncode == 0` 且 `tests == 0`），以后谁想把守卫简化成只看退出码会撞上它。
- **版本形状是测出来的不是猜的**：`node --test tests`（目录形式）在本地 node 24 是**失败**的；`node --test "tests/*.test.mjs"`（node 自己 glob）与显式文件列表都行。Python 守卫用**显式相对路径**（`tests/x.test.mjs`）——cmd 不展开通配符，而裸文件名 node 会当模块去找并报找不到（这条我第一次跑守卫就红了，是我自己的 bug）。类型剥离：用例直接 import `.ts`，node 22.6~22.17 需要 `--experimental-strip-types`，22.18+ 与 23+ 默认开；CI 钉的是 `"22"` 落哪个补丁号不由我们定，所以守卫里 major==22 一律带开关（实测 node 24 带这个开关照样 12/12 过）。
- **验证**：`tests/test_frontend_node_tests.py` 5 passed；`npm run test:node` **29 passed / 0 failed / 0 skipped**（原 12 + 本次 17）；变异三连成立；`harness.yml` 用 yaml 解析验证过（20 个步骤，新步骤在 `Build workbench frontend` 之后）。守卫用 `unittest.TestCase`，所以它会进 CI 的 `unittest discover` 主步骤，不依赖 pytest lane。
- **后半（同日）：把 Wave7 交给 Wave8 的那块补上**——`frontend/tests/liveAudioControl.test.mjs`，**17 项**，跑的是生产模块 `src/workbench/liveAudioControl.ts` 本体，零新依赖。核心是那条真机事故的契约：**停止说话 ≠ 停止推流**——`ended` 那一刻起还必须再推满 1 秒静音（实测 `tail` 期间 10 片），否则服务端 VAD 收不了句、turn 永不闭合（现象就是"转写完整却零回复"）。另外钉了：起说去抖要连 2 片、静音中途出声会重置 `silenceMs`、AI 说话时开口报 `bargeIn`、tail 期间再开口算下一句、`reset` 后去抖重新数、分片泵只产整片且余量留到下次、静音尾片恰好 3200 字节全零、`encodeAudioPacket` 的头/载荷形状与四类非法入参必抛、`pcm24` 是 24kHz PCM16 的格式名而不是 24-bit。现在全套是 **29 项**（原 12 + 新 17），Python 守卫靠"源码声明数 vs node 实跑数"自动跟上，不用改守卫。
- **新守卫的牙是这么验的（不碰仓库里的生产文件）**：把 `liveAudioControl.ts` 和测试复制到 `.tmp/w8mut/` 下，只在**副本**上把 `tailMs` 从 1000 改成 300 → 恰好 `核心契约：停止说话 ≠ 停止推流` 那一项 `not ok`，其余 16 项仍过。第一次尝试是直接改生产文件做变异，`finally` 里的还原撞上 `OSError [Errno 22]`（写入被占用），文件短暂留在变异态——已 `git checkout --` 还原并确认干净。**教训：跨进程写回有锁竞争时不要原地改被 watcher 监听的源文件，变异一律在副本上做。**
- **顺手发现一处可疑（属于 R2 owner，我没有代改）**：`resolveLiveModelAudioFormat('pcm24', 4000)` 会把越界的外报采样率**静默丢弃**并回落到 `pcm24`→24000，而模块文档声称要防的正是"按错速播放却看起来成功"。现在有一条用例断言【当前行为】并把这段理由写在测试名与注释里；要改成拒绝，删掉那条刻画用例即可变红，由 owner 决定。
- **未做（Wave8 剩下的那一半）**：**vitest + `@vue/test-utils`** 的 `.vue` 组件/接线守卫。试着装过一次，量化完代价后**已回退**（`package.json`/`package-lock.json` 现在是干净的）：`vitest@2 + @vue/test-utils + jsdom` 给这棵树带来 **1 critical + 3 moderate + 1 high** 的公告（critical 是 Vitest UI server 任意文件读；vitest 链不能升到 3.x，因为那要 vite 6/7，本树是 vite 5）。同时 `.vue` 挂载测试会直接踩在前端 lane 的 20+ 个未提交 `src/**` 改动上。所以这一步要么按"接受 dev-only 公告 + 不开 UI"推进，要么等前端收口，要么放弃 vitest 用已有的 node:test + Wave13 的 `dev_page_action`（真浏览器里点真组件）覆盖 `.vue`——第三条其实已经能验行为，只是不给组件级断言。


## 2026-10-01 Vibecoding 能力补全 Wave13：页面交互原语 `dev_page_action`（本会话，不占 R 槽位）

- **补什么**：vibecoding 循环缺的下半身。测得当时 101 个工具里**没有任何输入原语**（`click`/`type`/`press`/`wait` 零命中），`preview_project` 只能看画面——于是「点一下才复现」的 bug 只能靠模型想象。现在有了真实浏览器里的 click / type / press / wait / read / locate，走 CDP `Input.*`，页面收到的是原生事件（Vue/React 的 `input`/`click` 监听会真的触发）。
- **产物**：`agent_runtime/page_action.py`（工具 `dev_page_action`）、`tools.py` 的实现 + `_screenshot_data_url` + TOOLS 注册、`agent.py` 提示目录 1 行、`mcp_server.py` 归入 WRITE_TOOLS、`tests/test_page_action.py` **43 项**、门禁加 1 题（题集 17→18，基线已刷）。
- **启动路径只有一条**：会话持有的是 `visual_acceptance.browser_session()`（Wave7 抽出来的那个），**不自己 Popen、不引 playwright**——仓库已有硬规矩：浏览器必须出生在 kill-on-close 作业对象里（99°C 漏浏览器事故）。会话上限 3、空闲按 TTL 在下次调用时回收、进程退出 `atexit` 兜底；`close` 回报**真实**回收证据（`job_closed`/`profile_released`）。
- **每个动作都留可复核证据**：动作前后比对 MutationObserver 计数、console 错误数、失败请求数、运行时异常数、地址与标题，得出 `changed`/`navigated`/`reloaded`/`error_seen`/`no_change`；`error_seen` 判未通过，`no_change` 明确写「没有观察到变化，不要当成功」并提示改用 read/locate 取具体状态。
- **拒绝在派发之前**：命中多个却没给 `nth`、元素不可见、禁用、中心不在视口外——一律拒绝执行并**回候选清单**（含 tag/文本/尺寸/坐标/可见性），且断言「一个鼠标事件都没派发」。这是刻意的：点错东西还报生效是最贵的一类假干净。
- **测量才发现的两个真问题**（不是想出来的）：① 文本定位单级子串会把「加一」命中到「吃掉加一按钮」——改成**整串相等优先，落空才退到包含**，用例把这条钉住；② 页面导航会让 MutationObserver 重装、计数从 0 开始，差值变负数 —— 单独判成 `reloaded` 并把负值钳 0，不把「-7 次变更」这种证据喂给模型。
- **自查（review）这一轮真找到的东西**——先独立审（另一个 agent 读 diff 报问题），再逐条对着代码核实，最后用变异测试证明每条护栏真的有牙：
  - **HIGH：`press_key` 永远报不出 `no_change`**。动作前的快照是硬编码的 `{"href": row.url, "title": ""}`，而 `row.url` 停在 open 那一刻、标题恒为空 → 任何一次按键都会被比对成「标题变了/跳转了」，把没生效说成生效。改成动作前真读页面（`_current`）。
  - **HIGH：`no_change` 的 `ok` 是 True**。文字措辞诚实，但结构化消费方（MCP、工作流门）读的是 `ok` 字段——「点了没反应」在它们眼里就是成功。现在 `ok` 只在 `changed/navigated/reloaded` 时为真。
  - **MEDIUM：去重把重复报错藏了**。`_DevTools.console` 会去重也会截断，同一条报错每次动作都复现时长度差恒为 0 → 一直报错的页面被判成没问题。给 `_DevTools` 加了去重**前**的事件计数（`error_events`/`failure_events`/`runtime_events`），判定改用计数。
  - **MEDIUM：只在 open 时校验回环不够**。页内跳转/重定向能把浏览器带到外网机器。现在每个动作后复查 `href`，离开回环就 `left_loopback` + `ok=False` + 明说「请 close 后重新 open 本机地址」。
  - **MEDIUM：`type` 的证据读错字段**。读的是 `innerText`，而 `<input>` 的 innerText 恒为空——等于「插了字却无法证实插进去了」。改成 `value` 优先、空了才退回文本，并回 `value_landed`。
  - **LOW 三条**：`probe_installed` 恒为真（`int(x) >= 0`）→ 换成观察器是否真装上的 `mutation_observer`；会话上限只数已登记会话，两个并发 open 会各自看到 2/3 然后一起起浏览器 → 改成启动前先占名额（`_reservations`，失败路径必须归还）；`_sweep` 抱着全局锁关浏览器（taskkill + profile 回收可达数秒）→ 改成只在锁内除名、锁外回收。另：`read_page` 校验用 `"html"` 却用空串查询，空选择器的读取永远 found=false；`sanitize_flags` 黑名单原先大小写敏感，`--User-Data-Dir=` 就能绕过（Chromium 开关名不区分大小写）。
  - **变异测试 12 条全数被抓红**（歧义拒绝、隐藏拒绝、去重前计数、no_change 不算成功、离开本机要失败、名额预占、参数当字面量传、按键前真快照、导航后计数钳零、输入回读落点、回收证据在关栈之后取、黑名单大小写）。跑法见 `.tmp/w13_mutation.py`（临时脚本，不入库）。**其中一条是变异跑出来才看见的真 bug**：把 `preview_target` 挪到 try 之外的那版实现里，非回环地址抛错会漏还名额——所以 `test_open_refuses_a_non_loopback_url...` 现在还会断言 `_reservations` 归零。
  - **测试自己的两处空转**也修了：`test_error_seen_is_the_only_not_ok_verdict` 只断言字符串互不相等（恒真，什么都没测），换成用假页面走完整动作的 `ok` 语义表；假页面原先对 `value`/`text` 两种读取模式回同一个值，导致「读错字段」这条变异测不出来，改成按模式分别回话。
- **注入面**：页面脚本全是模块里的固定常量，参数以 JSON 字面量传入（`(js)(json)`）；不向模型暴露任意 JS 执行；selector 形状、长度、nth 范围、value 长度都有上限；只允许回环地址；截图读回时校验真实路径仍在项目内。用例包含带引号的注入载荷与 `@import`/`javascript:` 形态的选择器。
- **验证**：`tests/test_page_action.py` **49 项**（含 7 项真实浏览器 e2e：点击真的把「计数 0」变成「计数 1」、输入真的触发页面监听、导航判成 navigated、报错按钮判成 error_seen、隐藏/禁用/歧义全被拒且一个鼠标事件都没派发、close 后 `live_pages()` 为空且 msedge 进程数不变）；与视觉验收相关的 7 个套件一起 **187 passed / 82 subtests**（`test_media_fixture`、`test_dev_serve_preview`、`test_visual_acceptance_runtime`、`test_visual_console_signals`、`test_native_tools`、`test_mcp_server_expose` 全绿，说明 `_DevTools` 加计数没有回退既有验收行为）；门禁 **18/18** 且与基线持平无回退；`dev_page_action` 走文本协议真跑一遍 open→click→read→type→wait→locate→坏按钮→list→close 全对，截图作为图像附上；**变异测试 12/12 抓红**。
- **未做**：没有 drag/hover/scroll/select-file/对话框处理；`settle` 是「动作后观察多久」而不是条件等待（真正的条件等待只在 `wait` 里）；iframe 与 Shadow DOM 里的元素定位不到；一 session 一个浏览器，不共享登录态也不复用系统浏览器 profile；`dev_serve` 端口被抢仍不自动重试；前端仍**没有 eslint 配置**，`dev_diagnostics` 前端只有 typecheck 一条腿。


## 2026-09-30 漏浏览器（99°C）事故的第二半：回收必须「归属可证」，不能靠时间和事后补绑（本会话，不占 R 槽位）

- **触发**：用户报「cpu 突然99度」。实测 462 个 `msedge.exe` / 19.1 GB RAM / 100% CPU（内核态 49%）/ 内存只剩 2.5 GB / pagefile 已用 13 GB；Ryzen 9955HX Tjmax 95°C，早就在硬降频。全部是 `browser_session()` 起的 headless Edge：父 Python 被硬杀（会话重启、工具超时）时 `finally` 跑不到，一次留 16 个进程 + 一套 profile，一夜攒 40 多套。第一半（作业对象 + 重试删除 + sweep）由另一条 lane 连我的改动一起提交成 `1bb0725`；**这一节是它留下的三个洞，提交在 `4ea51ce`**。
- **洞 1：事后补绑有约 150ms 硬窗口。** `Popen` 之后再 `AssignProcessToJobObject`，一旦 Chromium 建好自己的 job 就永久返回 `ERROR_ACCESS_DENIED`，而失败被折叠成和「平台不适用」同一个 `None`。实测：0/50 ms 能绑上，150/300/600 ms 全部 `gle=5`；**失败概率随 CPU 饱和度上升，正是事故当时的条件**。改用 `CreateProcessW` + `STARTUPINFOEX` + `PROC_THREAD_ATTRIBUTE_JOB_LIST` 让浏览器**出生即在内**（新函数 `spawn_browser_in_job()`），窗口归零；绑不上就抛 `BrowserSpawnError` 而不是继续跑。`_reap_with_parent` 已删除，三个启动点（`browser_session` / `cockpit_live_validation.ProjectBrowser` / `workflow_preview_smoke`）走同一条路径。
- **洞 2：拿目录 mtime 当「多久没用」是错的。** Windows 上往**已存在**的子目录反复写文件不会推进父目录 mtime，而 Edge 平时正是往 `Default\Cache\` 里写。实测一个**正在使用**的 profile 被判成「7 小时没动过」，222 个文件删到 95 个，而 `sweep_stale_profiles()` 返回 `[]`——报「没什么可清理」的同时损坏了别人正在跑的会话。现在两条例据都要成立才动手：`.docmind-owner` 里的 PID 已没，**且** `DevToolsActivePort` 那个端口连不上；读不到端口文件就当作还在用。
- **洞 3：临时状态目录根本没人回收。** `config.py` 测试隔离的 `STATE_ROOT` 每个进程 `mkdtemp` 一次，攒到 **1,794 个目录 / 239 MB / 18,966 个文件**（里面是 `.chroma` 与 sqlite）。新增叶子模块 `temp_state.py`（只依赖标准库）统一「归属标记 + 按 PID 回收」，`config` 注册 `atexit` 自清、`dev_eval` 补标记。**实测一轮 pytest 新增 0 个**。历史遗留用重命名探针一次性回收（Windows 上目录内有文件被打开时改名会失败）：删 1,367 个、释放 239.8 MB。
- **花过真代价、别再重推的实测事实**：① `Popen` 拿到的那个 `msedge.exe` 是 launcher，**约 200ms 内以退出码 0 自行离开**，所以 `taskkill /PID <它> /T /F` 返回 rc 128、**从来没杀掉过任何东西**——作业对象是唯一在干活的收尾机制。② 只 `CloseHandle` **就已经是同步的**（关句柄后下一条语句用 ctypes 查 16 个已知 pid，全部已没）；`TerminateJobObject` 加上去测不出差别，所以没采用。③ Chromium **不会**在浏览器 root 死掉时自己终止整棵树（实测只杀 root 留 17 个存活），别把这句话当兜底写进注释。④ `TemporaryDirectory.cleanup()` 在 3.13 上**可以重试**（只是不带等待），早先注释说「只执行一次」是错的。⑤ `.venv/Scripts/python.exe` 是重定向壳，`Popen.pid` ≠ 脚本里的 `os.getpid()`，跨进程比 PID 必须先确认这一点。
- **测量陷阱（踩过两次）**：用 PowerShell 数进程单次约 1.15 秒，任何「当场为 0」的断言经它测量都会退化成永真——上面 ② 就是这么被误导的。即时性断言要用 ctypes 查已知 pid（`temp_state.pid_alive`），不要用 shell 计数。存活判断现在全仓只有一份实现（`visual_acceptance._pid_alive` 是它的别名），别再长出第二份——这轮已经抓到过同包内两套作业对象实现漂移的先例。
- **启动纪律门改成按文件白名单**（`tests/test_visual_acceptance_runtime.py::BrowserLaunchDisciplineTests`）。旧设计按守卫 token 逐文件豁免，**实测无牙齿**：往已有合规调用的文件里注入裸 `Popen([visual._edge_binary(), …])`，门照样放行。新设计只允许 `agent_runtime/visual_acceptance.py` 触碰裸 spawn 原语；argv 提到变量靠**传递闭包**追（单层会漏 `binpath = _edge_binary()` → `argv = [binpath]` → `Popen(argv)`）；探针字面量一律用类属性拼接构造，否则门会因为自己的样本而红；另加「探测器必须仍能看到那个唯一实现」的自证，用来区分「干净」和「失明」。
- **三条守卫都做了变异验证**（不是「跑过了」而是「拿掉会红」）：去掉 `EXTENDED_STARTUPINFO_PRESENT` → 出生断言红；去掉 `JOB_OBJECT_LIMIT_KILL_ON_CLOSE` → 16 个 pid 存活、红；去掉 `atexit` 注册 → 子进程退出后目录还在、红。硬杀 e2e：`browser_session` 与 `workflow_preview_smoke` 各自的真 `main()`，16 进程树 → 只杀 owner（**不带 `/T`**，否则是 taskkill 替被测机制干活）→ 存活 0；对照组关掉作业对象则稳定留 11 个。
- **验证**：`tests/test_temp_state.py` 5 项、`tests/test_cockpit_live_reaping.py` 4 项、`tests/test_visual_acceptance_runtime.py` 15 项；相关子集 **134 passed / 43 subtests**。`cleanup=` 出口让 `browser_session` 把「没起成 / 没关掉 / 没删净」交回调用方，`capture_project_preview` **只在偏离时**挂 `report["cleanup"]`（不污染 `ok = all(checks.values())`）。
- **未做 / 遗留**：① `windows_sandbox.py` 里还有第二套作业对象实现（`enterprise_sandbox` / `generated_adapter_runtime` / `process_runner` 三个消费方），收敛它是独立一轮，别和浏览器捆绑。② `mcp_autoconnect` 的 playwright 持久上下文留 **2,109** 个 `docmind_ac_*` 目录：进程侧其实是干净的（`ctx.close()`/`pw.stop()` 都有），漏的是调用方自持的 `user_data_dir`；**按 TTL 扫掉是错的**——`_launch_edge` 明写这个目录就是为了跨崩溃/跨人工步保住登录态，要先定策略（关闭即删 = 登录态作废，还是注册表 + TTL）。③ 554 个提权会话留下的空目录（0.0 MB）非提权 shell 删不动，只剩条目，别再追。④ `workflow_preview_smoke` 的 profile 那一半仍靠 `with TemporaryDirectory`，没有退避重试，该前缀也不在 sweep 范围内。⑤ 门看不见仓根，也认不出 `launch_persistent_context(` 这类非裸原语。

## 2026-09-30 Vibecoding 能力补全 Wave7：合成媒体夹具（`dev_media`）——R12 的设备依赖降到只剩听感（本会话，不占 R 槽位）


- **补什么**：backlog 里那条「没法把合成麦克风/摄像头内容注入浏览器」——R12 的语音回路每次都要人对着真设备说话才验得到，「静音尾」「音频先于视频」这类契约无法回归。现在输入变成了一个**确定性文件**：自己生成的 WAV/Y4M，经 Chromium 自带的假设备开关喂给**真实浏览器**的 `getUserMedia`，再由一个懂 R0 协议线的回环探针服务逐包判定。
- **产物**：`agent_runtime/media_fixture.py`（工具 `dev_media`，`action: build|run`）、`agent_runtime/visual_acceptance.py` 抽出可复用的 `browser_session()` / `serve_static()` / `stop_static()` / `sanitize_flags()` 与 `_DevTools.set_timeout`、`preview_project` 新增 `flags:`（把假设备开关交给项目自己的页面）、`tools.py` 实现 + TOOLS 注册、`agent.py` 提示目录 2 行、`tests/test_media_fixture.py` **54 项**、门禁加 1 题（题集 16→17，基线已刷）、`requirements.txt` 显式声明 `websockets`（裸 `uvicorn` 不带它，探针直接实现 WS 协议）、`mcp_server.py` 把 `dev_media` 归进 WRITE_TOOLS（会落文件、开监听端口、起浏览器）。
- **先测量再设计**（省掉了 Playwright 与浏览器内核下载）：实测 headless Edge 的 `--use-file-for-fake-audio-capture=<wav>` 真的把我们的 WAV 变成了麦克风内容——440Hz、幅度 0.4 的正弦经 48k 采集回到线上是 **RMS 0.2828**，与解析值 `0.4/√2` 一致；静音段实测**精确 0.0**；`--use-file-for-fake-video-capture=<y4m>` 同样成立（`getSettings()` 回 width/height/frameRate 与我们的 Y4M 头一致，逐帧亮度差在画布回读里可见）。假设备会**循环播放**夹具文件，所以 pattern 匹配按循环窗口算而不是硬对齐。
- **判定的是线上可观测的事实**：片形是不是 16k mono 100ms（3200 字节）、内容对不对得上夹具、序列单调、`captured_at` 在协议新鲜窗内、说完之后是否仍推 ≥10 片静音尾、静音尾有没有让探针回 `model.delta final:true`、音频包是否先于视频包（探针页面照抄生产 `sendNativeAudioReady` 的规则：任何视频帧出门前先推一片 3200 字节静音）。畸形包记成 `violations` **而不是被静默丢掉**——「假干净」是这一波最贵的错。
- **边界写清楚，别越界宣称**：探针页面**刻意不接能量门**（逐帧无条件推流，内容对齐才是确定的），所以这条回路验的是「采集 → WS → R0 解析」，`createVoiceGate` 本身不在这里跑；`media_fixture` 只断言夹具的逐帧能量确实跨过 `startRms=0.02`/`endRms=0.012`（即「喂给真门的信号形状是对的」），门限逻辑归前端单测（Wave8）。真机听感、回声消除、蓝牙热插拔仍然只有人能判断。
- **几处刻意的取舍**：① 探针服务复用 `realtime_protocol.parse_binary_packet`/`server_event`，验线上契约而不是造私有回声；只绑回环，`MediaProbe(host="10.0.0.1")` 直接拒。② 夹具默认写进 `.docmind/media-fixture/`（已 gitignore），显式 `out:` 才落项目可见处，且必须过越界/`-`前缀检查——与 Wave12「产物不弄脏仓库」是同一条纪律。③ 浏览器启动路径抽成 `browser_session()`：两条启动路径各自演化，迟早出现「预览能过、媒体过不了」这种解释不了的差集。④ `flags:` 会以 argv 直接给浏览器，但入口仍要自己拦：只接受 `--开关`，`--user-data-dir`/`--load-extension`/`--inspect`/`--remote-debugging-pipe` 一律拒绝。
- **实测发现并修掉的真缺陷**（三个都是「会误报通过/失败」级）：① `sanitize_flags` 原本把反斜杠当非法字符，Windows 上**每一条夹具路径都会被打回**；改成只拒换行/CR/NUL 并补了用例。② `_DevTools.drain` 为不卡住把 recv 超时压到 0.3s 却不恢复，之后的 `evaluate` 可能假超时；现在 drain 结束自动恢复。③ 跨段的 128 样本帧（半帧说话半帧静音）被算进「静音段最大能量」，导致 `closes_gate` 对**任何**夹具都失败；改为帧首尾同段才算干净帧，实测剔除 1 片跨界帧。另外 `audio_before_video` 一开始判不过，查明是**我页面自己的竞态**（视频首帧 120ms 与音频首片 ~100ms 抢跑），不是浏览器或生产逻辑的问题——补上生产的 audio-ready 规则后 100% 稳定。
- **验证**：`tests/test_media_fixture.py` **54 passed**（含 5 项真实浏览器回环，整套 46s）；门禁 `python -m agent_runtime.dev_eval` **17/17**（新题 `media-fixture-closes-without-a-device`，不启动浏览器也能跑，基线已重刷）；`dev_media action: run` 在本仓真跑一次 **11/11 项通过**，pattern 与夹具 100% 对齐、speech 中位 RMS 0.2828、最长静音连段 14；`tests/test_dev_serve_preview.py` 26 项在 `visual_acceptance` 重构后仍全绿；全量 pytest 见下方补记。
- **未做**：① R12 用它验的是「线」不是「听感」，真网关（`api.py` 的 `live_vision_stream` + DashScope）那条路仍要人跑一次才算完整闭环——探针是测试替身，不接 provider。② 屏幕共享 `getDisplayMedia` 没有假设备路径（只做麦克风与摄像头）。③ 浏览器那 5 项依赖本机有 Edge：GitHub ubuntu runner 是否装了 msedge **没验证**，缺浏览器时那几条明确 skip 而不是假通过。④ 探针页面与生产 `handleLiveAudioFrame` 是同形不同码（不共享实现），把它换成项目真页面的路要靠 `preview_project(flags:)`，那条只测到了「能拿到麦克风」，端到端跑真开发舱语音回路属 R12。

## 2026-09-30 Vibecoding 能力补全 Wave12：`dev_propose` + `dev_ci_status`（本会话，不占 R 槽位）

- **补什么**：交付的后半环。命令黑名单拦住了 `git commit/push`（提交与推送是人的决定，这没改），但代价是 Agent 只能口头描述「我改了什么、CI 怎么样了」。现在有了两份能交给别人看的东西。
- **产物**：`agent_runtime/proposal.py`（工具 `dev_propose`）、`agent_runtime/ci_status.py`（工具 `dev_ci_status`）、`tools.py` 两个实现 + TOOLS 注册、`agent.py` 两行提示、`tests/test_dev_propose_ci.py` **28 项**，并给 Wave11 门禁**加了 2 道题**（题集 14→16）。
- **`dev_propose` 的三条硬约束**：① 只跑只读 git（`rev-parse/status/diff`，复用 `code_intel._run_git` 的白名单与有界执行），绝不 add/commit/push；② 产物只写进**项目状态目录**（`project_state.path(root, "proposals/...")`），**绝不写进仓库工作树**——多人同仓时把临时文件丢进工作树就是给别人埋雷，用例用 `dev_git_diff` 自己当探针断言打包前后仓库状态一模一样；③ 未跟踪文件【不伪装成补丁】，只在草稿里列成清单并写明「不在补丁里，需确认后再入库」。
- **产物真的能用**：`test_generated_patch_replays_byte_identical` 把生成的补丁喂回 `patch_apply.apply_patch`，打到同一基线的镜像目录上，逐文件比对**字节一致**——不是生成一份好看但没人能用的文本。
- **`dev_ci_status` 的诚实边界**：只读 GET，绝不触发/重跑 workflow；没 token / 401 / 403 / 404 / 连不上分别回不同原因，且断言里明确禁止出现「通过」字样。**这个沙箱出不去 github（`git push` 实测 443 被拒），所以判定逻辑全部用可注入 transport 覆盖**（假传 200/401/404/500/ConnectionError + 日志解析），真联调要等用户机器；这一点不算做完，写在这里。
- **顺手修的两个真缺陷**：① `repo_slug`/`current_head` 原来只当 `.git` 是目录——worktree 的 `.git` 是**文件**（`gitdir:` 指针），而 remote 定义在**共享 config**（`commondir`）里，照原样读会让 Wave2 的 lane worktree 直接查不到仓库归属；现在按 gitdir→commondir 正确解析，用例覆盖（`git worktree add` 建真 worktree 验证）。② 查询串拼接会重复塞 `per_page`。
- **门禁自己咬了我一口**：加了 2 道题没刷基线，`test_shipped_baseline_matches_the_dataset` 立刻以 `new_cases` 报红——这正是 Wave11 那条设计的用途（防止题集与基线悄悄分叉）。刷完基线后 15/15 绿，`python -m agent_runtime.dev_eval` → **16/16 通过**。
- **验证**：Wave12 用例 28 passed；`tests/test_dev_eval.py` 15 passed；门禁 16/16；全量 pytest 见下方补记。
- **未做**：不建 PR、不推分支、不重跑 workflow（都是有意的，但意味着「一键交付」还差最后一步，那一步归人）；`dev_propose` 不产 `git format-patch` 式的多提交系列，也没把未跟踪文件内容嵌进补丁；`dev_ci_status` 只读 runs/jobs/日志，不做队列等待或轮询（要等 CI 得自己再调一次）；真网络路径未验证。

## 2026-09-30 Vibecoding 能力补全 Wave11：dev lane 能力评测 + 基线 + CI 门（本会话，不占 R 槽位）

- **补什么**：评测只有检索（retrieval_eval）与工作流（workflow_eval）两套，代码任务一条都没有——前九波加的那些工具（`dev_glob`/`dev_git_log`/`dev_find_references`/`dev_diagnostics`/`dev_patch`/`dev_lanes`/`dev_serve`/MCP server/内联投影）**到底可不可用、护栏还在不在**，此前只能靠感觉。本波把它变成会红的门。
- **形态照抄既有约定**：题集写在模块内的 `DEV_DATASET`（与 `workflow_eval.GAME_WORKFLOW_DATASET` 一致），基线是 JSON（`.github/dev-eval-baseline.json`），CLI 与 `--self-check/--baseline/--json` 同一形状；CI 里紧挨 retrieval gate 加了一步 `Run dev lane capability gate`。
- **每题跑两层，两层都要过**：① **脚本轨迹层**——fixture 代码库 + 固定 ReAct 步骤，走【真 Agent + 真工具回路】，再用 `agent_eval.score_record` 按 expect 打分；这层抓的是注册漏项、名字漂移、护栏误杀这类「模型根本调不到」的问题。② **效果层**——`verify` / `expect.files` 直接断言 **fixture 落盘成什么样**（哪些文件被改、哪些必须原样、哪些不该存在）。只测「调了对的工具」而不看副作用，补丁打一半、越界被放行这类失败照样溜过去。
- **明确不测什么**：不测模型智商。模型质量走 `run_golden.py --questions golden/questions_v2.json` + `agent_eval.py`（需要真模型、不确定、不进 CI）。本门是【harness 能力回归】，两件事别混。
- **14 道题覆盖**：按文件名找文件、TS 精确引用（注释与字符串不误报）、git 历史回答「谁改的」、诊断出真错误、诊断干净文件时确实说「没有发现问题」、补丁打对、**多文件补丁第二块不匹配时第一个文件必须原样**、补丁不许删文件、lane 认领冲突要点名对方 owner、lane check 回答归属、命令黑名单换个入口也不能绕、越界读取拒绝且不回显、MCP 出口默认只读且执行类永不暴露、连接器没打内联标记连握手都不发。
- **门禁自己必须会咬人（这比题目通过更重要）**：`tests/test_dev_eval.py` **15 项**里专门有——伪造一次回退（基线说这题过了、现在没过）必须 `regressed=True` 且 `main()` 退 1；题号重复／引用不存在的工具／没有 expect 都要 `validate_cases` 报错；**题集比基线多出题也要响**（防止加了题却不刷基线，让覆盖率悄悄漂移）；还有一条「能力覆盖面清单」断言：六类工具一旦从题面消失就红。
- **踩到并修掉的自身缺陷**：`steps` 最初存成**渲染好的 ReAct 字符串**，于是「这题到底动了哪些工具」查不出来——覆盖率断言和 `validate_cases` 全成了摆设；改成结构化工具步 + 回放时渲染。另外断言措辞也纠了一次：原本要求拒绝信息里必须出现 `a.txt`，而工具只点出对不上的 `b.txt`——**该断效果（文件没被改）而不是断文案**。
- **隔离**：每题一个临时 fixture 目录 + 临时 `config.STATE_ROOT`（lane 登记表、预算文件都落在那里），跑完还原 `code_root`/`STATE_ROOT`；用例专门断言跑完不会把真仓库写出 `a.txt` 之类的残留。git 类题在没有 git 的环境记为 skipped（不算失败也不算通过）。
- **验证**：`tests/test_dev_eval.py` 15 passed；`python -m agent_runtime.dev_eval --self-check --baseline .github/dev-eval-baseline.json --json` → **14/14 通过，regressions []，current_pass_rate 1.0**；harness.yml 解析 19 步含新 gate；全量 pytest 见下方补记。
- **未做**：基线只记通过与否 + 失败断言名，没记耗时分布（`min|max_actions` 已在 expect 里可用，但没进基线）；direct 类题（安全边界、MCP 出口）目前写在模块里而不是 JSON，若要给非作者编辑题目，需要把 direct 断言也做成数据驱动；`run_golden.py` 的 dev 题（真模型版）还没出，等有可用模型配额时补。

## 2026-09-30 Vibecoding 能力补全 Wave6：`dev_serve` + 预览可指向已运行的服务（本会话，不占 R 槽位）

- **先纠正范围（重要，别重造轮子）**：我原来把 Wave6 写成「缺 dev_serve + 缺浏览器观测」。**实测发现后半句是错的**：`agent_runtime/visual_acceptance.py` 早就在用 CDP 采 console（`_record_console`）和失败请求（`Network.responseReceived` / `loadingFailed` → `failed_requests`）并截图，`preview_project` 就是它的入口。真正缺的只有两件：① 它只会把项目目录当**静态站**临时服务，Vite/Vue 这类必须跑 dev server 的前端不 build 就预览不到；② 没有任何工具会**启动** dev server。本波只做这两件。
- **产物**：新模块 `agent_runtime/dev_server.py`（`free_port / loopback_url / parse_port_hint / probe / start / stop / render`）；`visual_acceptance.py` 新增 `preview_target(root, entry, url)` 并让 `capture_project_preview` 支持 `url:`；`tools.py` 新增 `dev_serve` + 扩展 `preview_project` 的 `url:` 入参；`agent.py` 提示两行；`tests/test_dev_serve_preview.py` **26 项**。
- **就绪判定不谎报**：先探 TCP 端口、再发一次 GET。**4xx/5xx 也算「已在响应」**（404 说明服务起来了，判成没就绪会逼 Agent 白等）；进程状态进入 `done/failed/timeout/cancelled` 立刻分开报告「进程已退出」而不是继续空等；「进程起来了但没探测到就绪」与「启动失败」是两条不同结论，前者 `ok=True + ready=False` 并回传日志尾部。`free_port()` 只是**建议端口**，从返回到子进程 bind 之间仍可能被抢，所以任何就绪结论都以实际连通为准。
- **安全边界**：`loopback_url` 只允许 `127.0.0.1 / localhost / ::1`（含 `127.*`），拒绝 `http://example.com`、`http://10.0.0.5`、`file://`、`ftp://`——**这不是网页浏览器**，不能被拿去访问内网/外网机器；命令复用 `_cmd_is_blocked`（`rm -rf /` 直接被拦下），进程 cwd 必须在 code_root 内（越界拒绝）；回收走既有 `process_runner.job_cancel`，后台任务上限仍是 8 个。
- **端到端证明（真浏览器，不是断言拼装）**：临时项目里放一个页面，内含 `console.error("boom-from-page")` 与 `<script src="/missing.js">`。`dev_serve` 起 `python -m http.server` → 就绪 → `preview_project(url=...)`：截图落地 3098 字节、`console_errors` 抓到 `boom-from-page` 与一条 `network: Failed to load resource`、`failed_requests` 抓到 `http_404 /missing.js`，**`ok=False`**——坏页面没有被判成通过，这正是这条链路存在的意义。
- **顺手做的可测试性重构**：把 URL/入口的决策抽成 `preview_target()`，这样「url 模式不需要 index.html」「非回环在启动浏览器之前就被拒绝」这两件事**不必真开浏览器**就能断言（避免 CI 上依赖 Edge）。`capture_project_preview` 现在调用它，行为不变。
- **发现并修掉的自身缺陷**：`dev_server.py` 初稿把进程状态写成 `finished/exited/killed`，而 `process_runner` 实际用的是 `done/timeout/failed/cancelled` → 秒退的服务会被当成「还在启动」白等到超时。已按真实状态名修正，并加用例 `test_command_that_exits_immediately_is_not_ready`。
- **验证**：Wave6 用例 **26 passed**；全量 pytest **2366 passed / 6 skipped / 0 failed**（另有二次确认跑）。
- **未做**：console/network 仍绑在一次 capture 里，没有「不截图只读日志」的独立动作（`dev_job_logs` 能读服务日志，但读不到页面 console）；没有自动等待某个选择器/DOM 状态（只有 `readyState==='complete'`）；没有页面内交互（点击/输入），所以「点一下按钮再截图」这条路仍缺；`dev_serve` 不做端口占用检测与自动换端口（只给建议端口 + 就绪探测）。

## 2026-09-30 Vibecoding 能力补全 Wave10：连接器工具一等公民化（本会话，不占 R 槽位）

- **补什么**：调外部 MCP 工具原本只有一条间接路——`dev_list_connector_tools` 先查、再 `dev_mcp_call(server, tool, json)`。模型看不见真实工具名与 schema，只能靠名字猜参数，连接器一多选择质量就掉。现在打了指纹的连接器工具会作为 `mcp__<server>__<tool>` 直接进本轮工具表。
- **产物**：新模块 `agent_runtime/mcp_bridge.py`；`agent.py` 在 `Agent.__init__` 里 `self.tools` 定型之后调 `mcp_bridge.attach(...)`（紧接「工具权威绑在实例上」那条评论之后，并保留同样的语义）；`tests/test_mcp_inline_tools.py` **34 项**。
- **默认零行为变化（这条最要紧）**：只有连接器配置里显式写了 `inline_tools: true` 的服务才会被投影；没打标记的**连 `tools/list` 握手都不发**（用例断言 `list_calls == []`）。总开关 `DOCMIND_MCP_INLINE=0` 可一键关掉，`status()`/`reset_cache()` 供排查。默认预设（godot/unity/unreal 等）都没打标记，所以装上这版以后所有会话的工具表长度不变。
- **上限**：单服务 `PER_SERVER=12`、总量 `TOTAL=24`、描述 `DESC_LIMIT=700` 字符；超限时在 notes 里指回 `dev_mcp_call`。理由写进注释：无界膨胀的 prompt 比没有工具更糟。
- **缓存**：`list_tools` 是真正的握手，每次构造 Agent 都问一遍会拖慢回合，所以按 `(root, 配置指纹)` 缓存，TTL 默认 300s（`DOCMIND_MCP_INLINE_TTL`），`refresh=True` 立刻生效。**改了配置能立刻看见**：指纹只哈希「谁打了 inline_tools / 是否启用 / transport / 是否声明只读」，加一个内联连接器会让指纹变化并重新握手（有用例）。
- **保守的能力标记**：`capability=NETWORK`（这样网络超时钩子与租约门继续生效）、`side_effect=MUTATING`、`parallel_safe=False`——连接器可能改外部世界，不能假设幂等；服务声明 `inline_read_only: true` 才降级为 PURE+parallel_safe。**HTTP 传输的投影标成 `group="web"`**，于是「联网开关」关掉时这些工具一起消失（`_web_blocked` 的既有语义），stdio 的不动。
- **不做跨连接器回退**：故意用 `mcp_client.call_tool` 而不是 `call_tool_with_fallback`——后者可能把一次有副作用的调用改投到别的连接器上重复执行。宁可响亮失败。
- **入参约定**：仍然是工作台统一的单字符串入参。JSON 对象直接转发；schema 里只有一个字符串字段（**必填或可选都算**——harness 自己的 server 把 `input` 声明成可选，只认必填会把 `dev_glob *.vue` 判成参数错，这是真跑出来的 bug）时裸文本当该字段；其余情况回一条带字段清单的说明而不是硬猜。异常/失败/空返回都转成文本，绝不抛出打断回合。
- **端到端证明（真进程，不是 mock）**：把 Wave9 那个 server 当连接器接进来——`mcp__docmind__dev_glob("*.vue")` 返回命中 `demo.vue`，`mcp__docmind__read_file("../outside.txt")` 返回「拒绝访问：不在代码根目录内」，**路径沙箱跨两次进程边界仍然成立**。过程中还逼出一个真设计缺陷：调用时重新读全局 `code_root` 会让工具作用在错误项目上，现在 root 在投影时就绑进闭包。
- **验证**：`tests/test_mcp_inline_tools.py` **34 passed**；**全量 2337 passed / 6 skipped / 0 failed**；`tests/test_native_tools.py` 的注册表/模式数量守恒未被破坏（投影只进实例，不进全局 `TOOLS`，用例专门钉住 `mcp__demo__search not in agent.TOOLS`）。
- **未做**：前端连接器面板还没有 `inline_tools` 开关（现在要手写进项目状态的 `mcp.json`），也没有 per-tool 勾选（只有整服务级 + `inline_read_only` 声明）；MCP 的 `outputSchema`/结构化结果没利用，只回文本；resources/prompts 侧没接；未与 `mcp_autoconnect` 的自动发现串联（自动发现的连接器仍需人工打标记才内联，这是有意的审批边界）。

## 2026-09-30 Vibecoding 能力补全 Wave9：harness 自己成为 MCP server（本会话，不占 R 槽位）

- **补什么**：harness 原本只有 MCP **client**（`mcp_client.py`）——DocMind 有 90 多个工具，但同一仓库里并肩干活的其他 AI 一个都调不到，只能靠公告表和 markdown 互相转述。现在它们能直接调。
- **产物**：`agent_runtime/mcp_server.py`（`python -m agent_runtime.mcp_server --project <目录> [--allow-writes] [--tools a,b] [--list]`）；`tests/test_mcp_server_expose.py` **15 项**；`requirements.txt` 加 `mcp>=2,<3`（此前只有 .venv 里有，没声明）。
- **默认只读**：`READ_ONLY_TOOLS` 12 个 = search_code / grep / read_file / list_dir / dev_glob / dev_git_diff / dev_git_log / dev_find_references / dev_diagnostics / self_verify / search_knowledge / dev_lanes。改代码的 5 个（apply_edit / create_file / dev_apply_edits / dev_patch / dev_move）必须 `--allow-writes` 才注册。
- **`NEVER_EXPOSED` 是硬名单，开了写权限也不给**：`run_command`、`python_exec`、`dev_mcp_call`/`dev_route_connector`（会二次放大权限的连接器跳转）、四个 `*_external_file`（越界文件操作）、`install_tool`、`dev_commit*`/`dev_rollback_changeset`、`start_workflow`。用例同时钉住这些名字**在清单里**，防止后来者顺手删掉守卫。
- **安全设计的关键点**：这不是新写一套执行路径——server 只是把 `tools.TOOLS[name]["func"]` 包一层，所以原有的路径沙箱、越界拒绝、区域写权限、审批门**全部继续生效**。`--project` 只是设 code_root，界的就是这个沙箱。传输用 **stdio（本机进程）**，不开网络端口，因此没有远程可达面；`openWorldHint=False`、`readOnlyHint/idempotentHint` 都按实际语义标注，别的 agent 的客户端能据此决定是否放行。
- **mcp 2.x 的 API 变化（踩过）**：`FastMCP` 已改名 `MCPServer`（`from mcp.server import MCPServer`），`mcp.server.fastmcp` 现在直接抛 ModuleNotFoundError 并让你钉 `mcp<2`。另外 pydantic 模型属性是 **snake_case**（`read_only_hint`、`server_info`、`is_error`），而构造时接受 camelCase 别名——写断言时别混用。
- **验证（真 client 走真传输，不是只测常量）**：用官方 `mcp.client.stdio` + `ClientSession` 起子进程连上去：默认态 `tools: 12`，`dev_glob("*.vue")` 命中 `demo.vue`；`read_file ../outside.txt` 回「拒绝访问：不在代码根目录内」；`dev_patch` 报 `Unknown tool`、`is_error=True`。加 `--allow-writes` 后 `count 17`、`dev_patch` 真把 `two` 改成 `TWO`、越界补丁被整体拒绝（文件没被创建）、`forbidden present: []`。`tests/test_mcp_server_expose.py` 15 passed；全量 pytest 见下方补记。
- **别人怎么接**：在别的 AI 的 MCP 配置里加一条 stdio server，command 指向本仓库 `.venv` 的 python，args `["-m","agent_runtime.mcp_server","--project","D:/WorkBuddy/rag-agent"]`；想让对面能改代码再加 `--allow-writes`。`--list` 可以直接看会暴露什么。
- **未做**：没接 HTTP/SSE 传输（有意不做——那会引入鉴权与远程可达面，需要单独设计）；没有 resources/prompts 出口（只出工具）；工具入参仍是单个 `input` 字符串，没给每个工具生成结构化 schema（与工作台 Action Input 同形，换来的是零漂移）；`mcp_server` 未挂进 `api.py`，起停由用户手动。

## 2026-09-30 Vibecoding 能力补全 Wave5：`dev_patch` + `dev_move`（本会话，不占 R 槽位）

- **补什么**：跨文件重构此前只有 `dev_apply_edits`（手写 old_text 块）；从 `dev_git_diff`、评审意见或别处拿到的**真 unified diff 没有入口**。`dev_patch` 补上，并且强制**全成或全不成**。
- **产物**：新模块 `agent_runtime/patch_apply.py`（解析 + 应用 + 移动 + 渲染）；`tools.py` 的 `dev_patch`/`dev_move` + TOOLS 注册；`agent.py` 工具目录两行；`tests/test_dev_patch_move.py` **35 项**。
- **匹配策略**：上下文与被删行**逐字相等**才算命中，不做模糊匹配——宁可整体报错也不改错地方；声明行号偏了但上下文一致时按距离由近到远自动对齐，并在 notes 里说明「实际落在第 N 行」。`@@` 纯新增（无上下文）就插在声明位置。
- **原子性（最重要的一条）**：所有文件先在内存里算完 + 过 `.py` 的 ast 语法检查，全部通过才逐个 `os.replace`；任一环节失败 → **一个字节都不写**，错误信息带上「补丁要的开头」与「文件实际那里是什么」的对照，方便直接改补丁。写盘途中失败会回滚已替换的文件（备份在内存里）。`test_multi_file_patch_is_atomic` 钉死：第二个文件对不上时第一个文件必须原样。
- **护栏**：路径必须落在代码根目录内（`a/`、`b/` 前缀自动去掉；`../`、绝对路径、盘符一律拒绝）；`+++ /dev/null` 的**删除操作一律拒绝**（删除要走带确认的删除工具，不许借补丁发生）；一次 ≤20 个文件；新建文件沿用 `create_file` 的 200KB 约定；**修改已有文件放宽到 4MB**——真代码上发现的：`agent.py` 与 `HANDOFF.md` 都超过 200KB，原来的统一上限会把合法 git diff 整体拒掉（尺寸风险在补丁大小而不是文件大小，故补丁正文另有 4MB 上限）；`.py` 打完必过语法检查。
- **`dev_move`**：被 git 跟踪的走 `git mv`（历史与 blame 延续，`--follow` 能追到改名前那笔——有用例验证），未跟踪的退回文件系统移动并**如实说明 git 会看成删除+新增**；目标已存在一律拒绝不覆盖；不删目录；移动后强制提示「用 dev_find_references / grep 检查旧路径 import，再用 dev_diagnostics 确认」。
- **纠正一条我自己说错的 policy 描述（重要）**：我之前几轮说过「`tools.py` 的 `_GIT_WRITE_SUBCMDS` 把所有 git 写子命令封死」——**不准确**。那个集合只封【历史类】：`commit/push/merge/rebase/revert/reset/clean/checkout/switch/apply/cherry-pick/stash/am/update-ref/worktree`，而 `git mv`/`git add`/`git rm`/`git branch` 本来就能通过 `run_command` 跑。所以 `dev_move` 不是「开了个例外」，而是**把已经能跑的操作收进有护栏的通道**；同理 `dev_lanes open` 用的 `git worktree` 是被封的，但它走的是 `process_runner` 直接调用（参数由代码拼、用户文本永不成为独立 argv），不是绕过 `run_command` 的检查。TOOLS 描述与 `agent.py` 提示里那句话都已改正。
- **验证**：`tests/test_dev_patch_move.py` **35 passed**；`test_native_tools`/`test_agent_code_tools`/`test_self_verify` 合跑 **103 passed**；**真代码回环**：把本会话 5 个提交（`4f1133b`/`d70cf9d`/`baac05a`/`2764092`/`3dcbb2b`）的 `git diff <base> <commit>` 在各自的基线副本上重放，25 个文件（含新建文件与 `.py/.md/.yml`）**逐字节一致、0 处不匹配**。全量 pytest 见下方补记。
- **未做**：不支持 rename 语义（`rename from/to` 行被忽略，请改用 `dev_move`）；不支持二进制补丁（明确拒绝）；`git apply --check` 那种「索引模式补丁」不支持；`dev_patch` 与 `dev_git_diff` 之间没有「只重跑失败 hunk」的增量修复回路。

## 2026-09-30 Vibecoding 能力补全 Wave4：`dev_diagnostics` 结构化诊断（本会话，不占 R 槽位）

- **补什么**：`self_verify` 只回「过 / 不过」+ 尾部日志，Agent 拿到一坨文本要么瞎猜要么重跑。本波把 ruff（Python）与 `npm run typecheck`（vue-tsc，前端）的输出归一成 **文件:行:列 + 规则号 + 严重度**。
- **产物**：新模块 `agent_runtime/diagnostics.py`；`tools.py` 的 `dev_diagnostics` + TOOLS 注册；`agent.py` 工具目录一行；`requirements.txt` 加 `ruff>=0.16,<1`；`tests/test_dev_diagnostics.py` **27 项**。
- **默认规则集是本波最重要的决定**：仓库里**没有任何 ruff 配置**，而 ruff 0.16 的默认规则会把上千条 `I001/UP035/PIE810/RUF010/FURB167` 风格提醒刷给 Agent，真错误被埋掉。所以默认 `--select=E9,F401,F811,F821,F822,F841,E722`（只查会咬人的），要全检显式 `rules: ALL` 或 `rules: E,W,F`；`rules:` 取值走白名单正则，`; rm -rf` / `--fix` 一律拒绝。
- **绝不假干净（三条响亮失败）**：① ruff 没装 → 「ruff 未安装（python -m pip install ruff）」；② 输出被窗口截断导致 JSON 非法 → 报「退出码说明有发现但输出没能解析」并附尾部；③ 前端 typecheck 退出非零却解析不出条目 → notes 里贴原始尾部。**曾经真实踩过**：`run_bounded` 默认窗口 4000+4000 字符，把 8786 字符的 ruff JSON 拦腰截断，工具静默回报「0 条问题」——这就是最危险的一类假干净；已同时加宽窗口（140000/40000）并加退出码规则，`test_unparsable_findings_are_reported_not_clean` 钉死。
- **真代码验证**：`dg.run(<repo>, 'agent_runtime/lanes.py', scope='py')` 报出 `10:1 I001`、`20:1 UP035`、`113:8 PIE810`、`319:45 RUF010`（默认规则集下 0 条会咬人的问题，说明本会话新增代码没有真实缺陷）；`frontend_diagnostics(<repo>)` 真跑 `npm run typecheck` → **exit 0、0 条、7.6s**，即当前前端类型是干净的，这个基线以后可直接对比。
- **护栏**：`target/config` 复用 `code_intel._contained_path`（越界拒绝；安全逻辑只有一份实现）；目标数 ≤12、条目 ≤200 并标注截断；只读——**从不传 `--fix`**，`--select/--config` 全部由本模块拼接、用户文本永不作为独立 argv 项；npm 路径支持 `DOCMIND_NPM_BIN`（与 `self_verify` 一致）；沿用「严禁 build，并发铁律」，只跑 typecheck。
- **严重度口径**：`E9*/F81*/F82*` → error（语法、重复定义、未定义名字），其余 E/F/W/C/B → warning，风格类 → info。`E722` 判为 warning 是有意的：它是卫生问题不是崩溃点。
- **验证**：`tests/test_dev_diagnostics.py` **27 passed**；全量 pytest 见下方补记。
- **未做**：eslint 没接（前端**根本没有 eslint 配置**，接入要先建 config，那是新增约定，留给用户拍板）；诊断未与 `dev_git_diff` 联动（例如「只报本轮改动文件」）；`rules: ALL` 在大仓库上会因输出过大退回 concise 逐行解析并如实标注可能不全。

## 2026-09-29 BUG 修复：共享屏幕后切页问 AI"看不到"（AI-B lane，ChatDock 帧附加放开）

- **用户报告**：共享整个屏幕后切换页面，再问 AI 能不能看到，AI 答"看不到"。
- **根因（代码级核实）**：开发舱在 App.vue 是 `v-if=everMounted + v-show`——切页不卸载 cockpit、共享持续推流；但"下一条消息附最新共享帧"的订阅与附加在 `ChatDock.vue` 都带 `scope==='cockpit'` 门（原 :1090/:473）。切页后驾驶舱聊天槽被隐藏，用户实际用的是普通工作台对话——那条消息没有任何画面，AI 只能诚实说看不到。属"驾驶舱专属对话"历史设计与"共享=一直在看"心智的冲突。
- **修复**：共享帧对**两种对话**都附加（`!attached && liveVisionFrame && imgs.length===0`，手动附图仍优先）；default scope 只订阅帧、不消费 visualTimeline/uiContext（R8 时间线通道保持 cockpit 专属）。与驾驶舱同一内存附件路径，无新增隐私面；停共享/切项目的清理逻辑原样生效。
- **验证**：前端 typecheck、build 通过。行为级验收需真实共享会话（自动化浏览器无可共享屏幕）：请用户复测"共享→切普通页→问'现在看到什么'"，应能描述当前屏幕内容。
- **冲突声明**：`ChatDock.vue` 上有 AI-F 未提交的桌面复验改动，本 pathspec 整文件提交会一并入库其 WIP，特此说明。

## 2026-09-29 Vibecoding 能力补全 Wave3：TS/JS/Vue 走 tree-sitter 精确引用（本会话，不占 R 槽位）

- **补的是什么**：`code_intel` 只有 Python 一条 AST 精确路径（`_py_matches`），其余一律 `_regex_matches`——而本项目最活跃的是 `.vue/.ts`（cockpit 一轮改 308 行）。也就是说 `dev_find_references` / `game_impact` 对 TS/Vue 基本是正则猜：注释、字符串字面量、对象键都会混进「引用」。
- **产物**：
  - 新模块 `agent_runtime/ts_index.py`：`ts_matches()` 用 tree-sitter 找定义与引用，`.ts/.tsx` 用 typescript/tsx 语法、`.js/.jsx/.cjs/.mjs` 用 javascript 语法、`.vue` 解析 `<script>`/`<script setup>` 块（按 `lang="js"` 选 flavor，多块逐个解析），**行号映射回文件真实行**。
  - `code_intel.find_references` 按扩展名分发；语法包缺失时 `ts_matches` 返回 None → 自动回退正则并在 notes 说明，**不静默丢结果**。
  - `requirements.txt` 加 `tree-sitter` / `tree-sitter-typescript` / `tree-sitter-javascript`（都有 linux wheel，CI 装得上）；`tools.py`/`agent.py` 的 `dev_find_references` 文案同步改写（**新行为必须写进提示，否则模型仍按旧语义理解结果**）。
  - `tests/test_ts_index.py`：**17 项**，`skipUnless(TS_READY)` 保证没装语法的机器不炸。
- **精确性用例（钉死相对正则的全部价值）**：注释里的同名文本不算引用；字符串字面量里的不算；`{ foo: 1 }` 这种**对象字面量的键**单列为低置信 `kind:"str"`（重命名要同步改，但不是读引用）；`live.state.foo` 记为 `attr`；`interface Shape { foo: string }` 判成定义；JSX 元素名算引用；Vue 的 `<!-- greeting -->` 模板注释**不算**引用而 `{{ greeting }}`/`@click="bump"` 算。
- **真代码验证**（不是夹具）：`find_references(<repo>, 'sendNativeAudioReady', scope=['frontend/src'])` → 定义 `AutonomousCockpit.vue:311` + 引用 `:1215`；`liveNativeAudioReady` → 定义 `:291` + 4 处引用（`:312/:316/:1073/:1323`），与 `grep -rn` 逐行一致，即**没有因收紧匹配而漏报**；`OUTPUT_SAMPLE_RATE` → 定义 `liveAudioControl.ts:19`。本轮扫描 114 个 TS/JS/Vue 文件。
- **三个踩过的坑（后人少跌）**：① 语法入口名不统一——`tree-sitter-typescript` 导出 `language_typescript/language_tsx`，而 `tree-sitter-javascript` 0.25 只导出 `language`，`getattr` 拿到**函数对象**而不是 capsule 时报 `TypeError: an integer is required`，已用 `_grammar_entry` 按候选名解析；② 解析截取片段时**字节偏移必须用该片段的 bytes**，用整文件的 bytes 会把每个符号都读错位（Vue 曾因此全部命中为空）；③ `str.splitlines()` 把 `\x1c-\x1e` 当换行，所以记录分隔符只能用 `\x01/\x1f`（与上一波同源）。
- **改动了别人的断言（如实通报）**：`tests/test_agent_code_tools.py::test_non_python_uses_word_boundary` 原写「非 Python 时 `const foo = 1` 算引用」，现在它被判成**定义**（作者意图——注释与 `foobar` 不算——保持不变并继续钉住）。已加注释说明改动原因，属行为改进而非回归；该文件作者 lane 如不认可可回滚这一条用例。
- **验证**：Wave1/2/3 测试 + `test_native_tools`（注册表/模式数量守恒）合跑 **140 passed**；全量 pytest 见本会话下一条补记（本机需 `--basetemp`）。
- **未做**：没接 pyright/BasedPyright 做**类型**解析（tree-sitter 只给语法，不给跨文件类型推断，例如 `foo` 在不同模块同名会各自成立）；`.svelte`/`.go`/`.rs` 仍在正则路径；`game_impact` 未复用新路径（它走 `symbols.py` 的 GDScript 索引）。这些留给 Wave4 之后评估。

## 2026-09-29 Vibecoding 能力补全 Wave2：`dev_lanes` 认领登记表（本会话，不占 R 槽位）

- **动机（本会话实测的摩擦）**：多人同树靠公告表和 markdown 排他，会**朝两个方向过期**——表上写 0% 而实现早已进树（R2/R7 就是这样），或者两个会话同时领同一块；我这半小时里 HANDOFF 本身被别的 lane 改了两次（`file has been modified since read`），而别人的收口提交会把谁的半成品卷进 HEAD 全靠运气。本波把「谁在动哪些文件」变成机器可查的**带 TTL 租约**。
- **产物**：
  - 新模块 `agent_runtime/lanes.py`：`claim / status / release / heartbeat / check / open_worktree / close_worktree` + `render()`。
  - `tools.py`：单个 `dev_lanes` 工具（`action:` 分派，避免把 7 个动作刷进工具目录）+ TOOLS 注册。
  - `agent.py`：系统提示工具目录补上 `dev_lanes` 一行。
  - `tests/test_lanes_registry.py`：**34 项**（unittest 风格，两个 CI 步骤都会收）。
- **规则**：排他按「最长无通配目录前缀」保守判定，`frontend/src` 与 `frontend/src-extra` **不算**冲突，`*.vue` 这类无目录前缀的模式等于占下全仓（会提示收窄）；`fnmatch` 的 `*` 跨 `/`，所以认领只会变宽不会变窄——对排他是安全方向。冲突**当场拒绝并列出对方 lane/owner/pattern**，正好是本项目「把问题清单发给 owner」的协议形状；明知无重复可 `force: true` 共管并在提示里留痕。TTL 默认 120 分钟（夹到 5..1440），过期即视为放弃、可被他人接管，`heartbeat` 只允许 owner 自己续租。重复 claim 同一 lane = 覆盖范围 + 续租。
- **worktree 是可选动作而不是前提**：`open` 在**项目状态目录**下开通独立 checkout（不落在 git 工作树内，`_worktree_base`），有同名 `lane-<name>` 分支就 attach，否则 `--detach`（绝不凭空占用分支、绝不 `--force`）；`close` 默认**拒绝删除有未提交改动的 worktree**，确认丢弃才 `discard: true`，且收回 worktree 不会顺手释放认领。认领表本身只写 `lanes.json`，不碰 git 历史。
- **护栏**：跨进程互斥用 `mkdir` 原子锁（Windows/POSIX 都原子），>3s 竞争直接抛错让调用方重试，>60s 的陈锁自动破；写盘走临时文件 + `os.replace`；`lanes.json` 损坏时另存 `lanes.corrupt-*.ts` 并重建空表而不是崩；lane 名白名单正则、禁 `..`；`owner/note/base_ref/paths` 一律拒绝以 `-`/`=` 开头或含控制字符；`_GIT_ALLOWED` 只留 `rev-parse|status|worktree`（`commit` 实测被拒，有回归用例钉住）。
- **验证**：`tests/test_lanes_registry.py` **34 passed**；**全量 pytest 2202 passed / 6 skipped / 0 failed（255s）**。中途一次全量出现 4 项失败全部落在 `tests/test_review_app.py`——该文件**从未入库**（`git ls-files` 无记录）且跑完即消失，属他人 WIP/服务重启，与本波无关，如实记录以免后人误判。
- **未做（如实说明）**：① `run_command` 的 cwd **没有**自动切到 lane worktree——那要改命令执行主路径，风险面太大，当前由工具把 worktree 路径回给 Agent 自己用；② 认领表只在 DocMind 侧，其他 AI（Cursor/Qoder 会话）要享受它得靠 Wave9 的 MCP server 出口或直接用这个工具；③ 未接 `git worktree prune` 的自愈，目录被手工删掉时 `close` 会清登记但不会跑 prune。
- **下一波（勿重复领）**：Wave3 TS/Vue tree-sitter 精确引用、Wave4 `dev_diagnostics`、Wave5 `dev_patch`/`dev_move`、Wave6 `dev_serve`/浏览器观测、Wave7 媒体夹具、Wave8 vitest、Wave9 MCP server、Wave10 MCP 工具一等公民、Wave11 dev lane 评测、Wave12 `dev_propose`/`dev_ci_status`。

## 2026-09-29 23:08 R12 provider 侧复跑验收（AI-B/AI-A 兼任）【本次两份报告已提交】

- 用户指令"进行 R12 真机检验"。在**本沙箱**用真实 Key + `qwen3.8-omni-flash-realtime`（音色 Jennifer）复跑 `verify_realtime_acceptance.py`，样本 `D:\Temp\docmind_jfk_sample.wav`（已存在，无需下载；**沙箱访问 raw.githubusercontent/github.com 仍被重置**，所以 push 依旧只能用户本机做）。
- **quick 冒烟 PASS**：e2e 3207ms、模型 255ms、done=True、转写正确、恢复重连成功。
- **全量 PASS**（报告 `docs/realtime-r12-acceptance-20260929-full.md`，quick 版 `-quick.md`）：首响应 3222ms、模型延迟 236ms、持续响应 2/3 轮、打断后新增 0 条（打断前已收 58 片）、全程重连 3 次均成功、错误率 0.0%（0/132）。**本轮抓到一次真实故障注入**：turn-2 两次 `stream_broken`，retry+重放机制按设计恢复（该轮最终未计入错误、turn-3 正常），实证网关"retryable stream_broken 后台恢复"三条硬要求在真实服务下工作。视频帧 accepted=True。
- 结论沿用报告"关键发现"：静音尾（~1s）是收句唯一手段、`commit()` 不能替代、成功判定只看 `done`、断连后必须重发。
- **仍未覆盖（诚实边界）**：设备侧（真实摄像头/麦克风采集、扬声器音质——尤其 `model.audio` 的 pcm16@24k 假设）与驾驶舱 UX 需要用户本人在场；本报告只刷新 provider 链路证据（此前 8f32730 报告的指标与本次一致，互相印证）。
- 本地提交两份报告 + 本节；`git push` 依旧被沙箱网络阻断，等用户本机执行（当前 main ahead 15+）。

## 2026-09-29 Vibecoding 能力补全 Wave1：`dev_glob` + `dev_git_log`（本会话，不占 R 槽位）

- **背景**：用户拍板把 vibecoding 能力缺口按波次全量补齐（清单见本会话前段；Wave 编号对应任务表）。本条是第 1 波，纯增量、只读、不动实时线。
- **补什么**：harness 此前「找文件」只能 `list_dir` 一层层翻或 `grep` 搜内容，「看历史」完全没有入口——`code_intel` 的只读白名单只有 `rev-parse|status|diff`，Agent 看不见「谁在什么时候改了什么」。这两件事在多人并行 lane 的协作里是刚需：判断是否重复劳动、确认某实现是否已入库、找回被覆盖前的写法。
- **产物**：
  - `agent_runtime/code_intel.py`：`glob_files()` + `render_glob()`（按文件名/相对路径通配，浅层优先排序，跳过 `_SKIP_DIRS` 但**故意不受 `_SOURCE_EXTS` 限制**，因为找 `Dockerfile`/`*.yml`/lockfile 正是它的用途）；`git_log_preview()` + `render_git_log()`（`paths/limit/since/until/author/at/files/timeout`）；`_GIT_ALLOWED` 增加 `log`（仍为只读）。
  - `tools.py`：`dev_glob`、`dev_git_log` 两个实现 + TOOLS 注册。
  - `agent.py`：系统提示工具目录里补上这两行（**新工具必须同步登记提示，否则模型不知道它存在**——这条对后续每个 Wave 都成立）。
  - `tests/test_dev_glob_git_log.py`：39 项（unittest 风格，两个 CI 步骤都会收）。
- **护栏**：`since/until/author/at` 取值经 `_clean_scalar`，以 `-`/`=` 开头或含控制字符一律拒绝（参数注入）；`paths:` 复用既有 `_validate_paths`（越界拒绝）；`scope` 走 `_contained_path` 同规则；pattern 含控制字符/超长拒绝；walk 有 50000 条目硬上限并如实标注不完整；`limit` 上限 500（glob）/ 200（log）。
- **两个踩过的坑（后人少跌）**：① 记录分隔符最初用 `\x1e`，而 `_strip_git_noise` 的 `str.splitlines()` **把 `\x1c-\x1e` 当换行**，结果整段历史被压成一条、其余提交变成「第一条的文件名列表」——改用 `\x01`/`\x1f`（不在 splitlines 集合内），并写了两条钉死该回归的用例。② 空仓库不是错误：git 的 `fatal: ... does not have any commits yet` 会被噪声过滤剥光，所以**不能靠措辞判断**，改为 probe `rev-parse --verify HEAD`，返回 ok + 「还没有任何提交」提示。
- **顺手修**：`_run_git` 保留 `raw_output`（未过滤原文），只用于失败消息——此前报错只剩「无输出」，Agent 拿不到 git 的原话。
- **验证**：`tests/test_dev_glob_git_log.py` + `tests/test_agent_code_tools.py` 合跑 **77 passed**；再加 `test_native_tools`（`len(tool_schemas())==len(TOOLS)` 这条会因漏注册而红）等 5 个文件 **121 passed**；**全量 pytest 2168 passed / 6 skipped / 278s 全绿**（本机需 `--basetemp`，见上一节 Windows 坑）。
- **未做（如实说明）**：原计划本波还想接 `dev_review`，查证后**主动放弃**——`independent_review.review_project()` 已由 `game_workflow.py:3227` 在工作流里调用，且 `execution_mode()!="enterprise"` 时直接返回 `local_mode`，再包一层工具是重复建设。开发机真正缺的是「看 diff + 跑测试」，那由 `self_verify`/`dev_git_diff` 覆盖。
- **后续波次（同一任务表，勿重复领）**：worktree 隔离 + lane 认领、TS/Vue tree-sitter 精确引用、`dev_diagnostics`、`dev_patch`/`dev_move`、`dev_serve`/浏览器观测、媒体夹具（R12 设备依赖）、vitest、harness 作为 MCP server、MCP 工具一等公民、dev lane 评测集、`dev_propose`/`dev_ci_status`。

## 2026-09-29 CI 盲区修复：pytest 风格测试首次进闸门（本会话，不占 R 槽位）

- **问题（实测，不是推测）**：`harness.yml` 的 `Run full test suite` 用 `python -m unittest discover -s tests`，而 unittest 只收 `TestCase` 子类，**模块级 `def test_*` 一律不收**。用 `.venv` 对本仓库实测收集：**1835 项 / 145 个文件，另有 22 个测试文件对 CI 贡献 0 项**——正好是实时/语音这条 lane：`test_realtime_*`(10)、`test_live_audio_control`、`test_live_stream_control`、`test_live_vision`、`test_live_vision_alerts`、`test_visual_feedback`、`test_visual_targeting`、`test_mcp_discovery`、`test_mcp_curated_index`、`test_capability_lease`、`test_react_token_filter`、`test_adapter_catalog`、`test_project_checkpoint_selective`。也就是说 R0–R14 的 **296 项断言此前从未进过 CI**。
- **另一半原因**：`requirements.txt` 里没有 pytest（只有本机 `.venv` 装了 9.1.1）；`tests/conftest.py` 的 autouse 环境隔离是 pytest-only，在 unittest 步骤里等于死代码——那份「防 600s 卡死」的保护只作用于开发机。
- **改法（两处，纯增量）**：① `requirements.txt` 加 `pytest>=9.1,<10`；② `harness.yml` 在 `Run full test suite` **之前**新增 `Run pytest-style lane`（仅 sqlite job）：用 AST 现场扫 `tests/test_*.py` 中含模块级 `def test_*` 的文件交给 pytest，并用 `test -n "$files"` 保证扫描为空时响亮失败。**不写死文件列表** → 以后任何 lane 新写 pytest 风格测试会自动进闸门，避免重演「表上 0% 而树里已实现」式的静默失明。原 unittest 步骤原样保留，不一次性把平台差异问题全翻出来。
- **验证**：
  - `python -c "import yaml; yaml.safe_load(open('.github/workflows/harness.yml',encoding='utf-8'))"` → 18 步解析通过；把该步 `run` 脚本原样抽出来在 bash 跑，heredoc 在 YAML 块标量里反缩进正确。
  - AST 扫描得到 **22 个文件**，与「unittest 收集 0 项」的实测集合**完全一致**（两条独立路径互证）。
  - 这 22 个文件跑 pytest = **296 passed**。
  - 过程中一次引号写坏导致 pytest 收到空参数，意外完成**全量 pytest 复跑：2129 passed / 6 skipped / 60 subtests，249s 全绿**。要不要把 CI 整条切成 pytest 由用户定（Linux 平台差异未验证，本轮不越权）。
- **已知坑（本机专属，不影响 CI）**：这台 Windows 的用户名含 `'`，pytest 默认 basetemp `D:\Temp\pytest-of-h'h'h` 直接 `PermissionError [WinError 5]`，表现为 23 errors + 1 failed。本机跑 pytest 必须带项目内 `--basetemp`。ubuntu runner 无此问题，故 CI 步骤里**不**写 `--basetemp`。
- **未完成 / 边界**：① 本会话无 push 能力（沙箱代理断 github），改动只在本地，要等用户 push 才真正跑到 CI；② postgres job 不跑该 lane（与现有测试步骤分布一致）；③ 未装 pytest-timeout，将来若有挂死测试靠 Actions 作业超时兜底；④ 音频先行闸门已由 `8cefac0`（前端）+ `d3e376c`（provider）收口，当前只剩真实摄像头/麦克风/扬声器条件下的 R12 设备验收。

## 2026-09-29 音频就绪闸门：真机对照验证（AI-D，只读，未改任何代码）

- 对象：`realtime_omni.py` 里那套 provider 侧闸门（`_audio_primed` + `send_frame` 首帧引导，已在 `d3e376c` 收口）、以及 `8cefac0` 的前端闸门。**我没有改动这两个文件**，只做真机验证给作者引用。
- 真 Key + `qwen3.8-omni-flash-realtime`，两个方向各跑 2 次：
  | 顺序 | 结果 |
  | --- | --- |
  | 闸门生效，先发帧再发音 | 0/2 报错，`frame_ok=True` |
  | **绕过闸门**（手工置 `_audio_primed=True` 后先发帧） | **2/2 复现** `Error append image before append audio.` |
- 结论：**闸门确实在抑制这个错误，且该错误可稳定复现**——不是偶发，也不是探测方式测不出来。AI-G 设备报告里那条 P1 阻塞（`docs/realtime-r12-device-acceptance-20260929.md`）在代码层面已被解除，**现在 R12 只剩"必须有真实摄像头/麦克风"这一条环境阻塞**。
- 建议收口时把「绕过闸门必须仍能复现原错误」写成回归用例，否则这个闸门以后被误删没人会发现。
- 另注：本地较 `origin/main` 多 8 个他人提交尚未推送（远端仍停在 `8f32730`），我没有代推别人的 WIP。

## 2026-09-30 R15 评审优化：核实入口的上下文修正（③）+ 忽略期内不再弹（①）（AI-F）

R15 已由实现者落地（R15-0 决定 + R15-1/2/3），我做了独立验证后按价值排序做了两条优化。

**③ `reviewRealtimeAlert` 不再复用 `app_interface_inspect`（功能错误，不是体验瑕疵）**
那段系统提示写的是「本轮用户意图**就是**触发『浏览当前界面』，请直接执行，不要回答『没有新问题』」——
与 prompt 里"核实这条提醒"**互相矛盾**，会把 Agent 带成泛泛扫一眼界面。而这是 C **唯一的用户主动
入口**（其他都是内部自动行为），每次点击都受影响。现在新增专用 `uiContext=verify_realtime_alert`：
提示点明该提醒是实时模型自述、**未核实、可能幻觉**，附件是**报出它那一刻**的画面（不是当前画面），
要求围绕这一条核实；确认没事或证据不足就直说没找到证据、**不要为了交差而编造问题**、不要泛泛浏览界面。

**① 忽略过的提醒在窗口期内不再弹**
后端相似度去重只有 45 秒窗口，过了窗口同一处会再弹 —— 用户刚点完「忽略」又看到同一条，会以为按钮
没生效。新增两个纯函数（`liveStreamControl.ts`）：`pruneDismissedVisualAlerts()`（10 分钟窗口、
最多 20 条、超窗自动裁）与 `isDismissedVisualAlert()`（复用 `sameVisualFocus` 判"同一处"，换说法也认）；
忽略时记下这一处，新提醒到达时先查。**带时间窗是刻意的**：10 分钟后同一位置真的又出问题，仍应能提醒。

**未做（建议等 R15-3 误报/漏报数字再定）**：② 同一轮多条异常被 2s 全局节流砍掉——一轮里报两个
**不同**异常很罕见，改动要动网关合并逻辑 + 补测试，性价比最低。

**验证**：`test_live_stream_control.py` + `test_voice_turn_context.py` **7 passed**（新增：忽略判据的
四个方向 + 窗口过期 + 有界裁剪；`verify_realtime_alert` 的提示不得继承"就是浏览界面"那段）；前端
`npm run build`（含 typecheck）通过；全量套件见提交信息。

## 2026-09-30 【任务表】C：实时模型「发现」回流 —— 待调研后由 4 个 AI 分工（**未认领**）

用户确认「C 也可以做，只是需要调研」，并要求发出任务表由多个 AI 分工。画布已加 **波次 4 / R15-0~R15-3** 与公告表「待分派」行。以下是给接手者的完整背景（含我判断的坑）。

**为什么做 C（边际价值）**：A 档旁路观察（`DOCMIND_NATIVE_OBSERVE_INTERVAL`，默认 5s）已经把「主动发现画面问题」接回来了，但它走**视觉模型**、每 5s 一次、且要额外调一次模型。C 走**实时模型自述**——它本来就在看画面，所以**实时且零额外调用**；代价是自述可信度更低（可能幻觉）、且需要一条新通道。

**R15-0 调研（先做，其余三项都依赖它）**——三个必须先定的问题：

1. **载体**：
   - (a) 复用 `video.observation` 加来源标记（`source: "realtime"`）：**不改协议**，但语义混用（客户端会把它当视觉模型观测处理），且**有踩雷风险**——客户端有一条「`ok:false` 或 `audit.mode=unavailable|error` → `socket.close()`」的分支（为抽帧模式写的），回流事件必须确保不触发它；
   - (b) 新增服务端事件类型（如 `model.observation`）：语义干净，但**必须改 R0 独占的** `agent_runtime/realtime_protocol.py` 与 `frontend/src/workbench/realtimeProtocol.ts` → 需用户改派或 /root 执行。
   → 这一条决定后面几个人能不能开工，所以先定。
2. **触发**：怎么从实时模型的自由文本里认出「发现」？建议让它用**固定前缀自称**（如「【疑似异常】…」，靠 instructions 约束），而不是网关猜关键词（脆）；备选是每轮都落（噪音大，会把对话文本灌进时间线）。
3. **消费方**（风险递增）：①面板告警横幅（复用 `liveVisionAlert` 的「交给 AI 排查」）②R8 时间线（进主 Agent 上下文）③**自动起一轮 agent**。③最激进——模型一句幻觉就自动开工，我不建议默认开。

**其余分工**：
- **R15-1 网关回流管线**（`agent_runtime/realtime_bridge.py`，AI-F 的模块）：按标记从 `model.delta` 抽自述 → 生成回流事件；**未核实标注、按文本相似度+时间窗去重、限长、节流**。验收：同一条异常说十遍只进一条。
- **R15-2 前端呈现与安全**（可与 R15-1 并行，接口按 R15-0 定）：渲染成「实时模型的未核实提醒」，**与抽帧观察视觉上区分**；必须绕开上面那条 `socket.close()` 分支。
- **R15-3 准确性与回归**：假 provider 造「说了异常/没说异常」两类样本 + 真机抽查，给出**误报/漏报数字**；误报率超阈值就默认只进面板、不进上下文。

**红线**：R15-0 没定之前不要动手实现；如果选 (b)，改 R0 协议文件需要用户改派。

## 2026-09-30 原生模式下的「主动发现画面问题」接回来（A 档旁路观察）+ 实时模型主动报异常（B）（AI-F，用户指派）

**问题**：用户察觉「对话 agent 没有调用能力，那能发现画面里有问题的地方吗」。核实后确认**三条看画面的路能力完全不同**：

| | ① 抽帧观察 | ② 原生实时模型 | ③ 转交时附带的单帧分析 |
|---|---|---|---|
| 谁在看 | 视觉模型 `analyze_live_frame_ep` | omni 模型 | 主 Agent 自己的视觉模型（`analyze_images`） |
| 产出 | **结构化**：observations + **anomalies** | 纯自然语言 | 结构化 `vision_context` |
| 异常告警 | ✅ `live_vision_alerts` → 面板横幅 | ❌ | 只对那一帧、只在你发话时 |

**而 `api.py` 的 `if native_taken: continue` 让 ① 在原生模式下根本不跑**——那个专门做告警的机制只挂在抽帧路径上。所以原生模式下**"被动问它在看什么"能看到，"主动发现问题"是断的**。

**A（本提交）**：原生模式下保留**低频旁路观察**，频率可配 `DOCMIND_NATIVE_OBSERVE_INTERVAL`（默认 5s，0 = 关），只为异常告警与实时时间线。
- 关键安全点：旁路是**附加**的，失败**绝不上线**。前端有一条分支——收到 `ok:false` 或 `audit.mode=unavailable|error` 的 `video.observation` 就 `socket.close()`（那是给抽帧模式写的），而原生会话上还挂着语音，被一次旁路探针误杀的代价太大。所以 `process_frames(native_probe=True)` 在这些情况下直接 return，一个字都不发。
- 顺带把 `_NATIVE_OBSERVE_LAST` 在会话收尾时回收（`_LIVE_VISION_LAST/_CLIENTS` 同类的不回收坑，别再犯）。
- 频率可配 + 旁路语义 ⇒ 成本可控：默认每 5s 一次视觉模型调用（抽帧模式是 1.4s 一次）。

**B（本提交）**：`DEFAULT_INSTRUCTIONS` 加一段——看到报错弹窗／可见报错文字／明显错位遮挡时**用一句话主动提醒**，并说清位置；**只说实话、不要凭推测报异常**（自述属未核实资料，误报比不报更伤信任）。

**验证**：`tests/test_realtime_gateway_bridge.py` **30 passed**，新增 3 条——旁路观察会跑且**帧仍直送 provider（旁路≠降级）**、探针失败**一个字都不上线**（含 `analyzer.calls == 1` 防空转）、开关设 0 时不再调用视觉模型。**反向验证承重**：去掉 `native_probe` 守卫后，那条危险事件（`ok:false, audit.mode=unavailable`）确实上线了 —— 正是会让前端关掉整条会话（连语音）的那条。`test_realtime_provider.py` 的人设断言补上 B 两处措辞。全量套件 **2340 passed / 6 skipped / 0 failed**。

**测试夹具的一处必要调整（说明，不是放宽断言）**：旁路观察会改变原生会话的线上事件序列，本文件多数用例断言的是精确序列，所以 autouse 夹具**默认关掉**它（只有专测它的用例自己打开），并清 `_NATIVE_OBSERVE_LAST`（本文件共用同一 project_id，不清会互相压制——这是这个文件反复踩的老坑）。

## 2026-09-30 语音轮次：噪音门控 + 框架说明移出用户消息 + 回答形态约束（AI-F，用户「按你的建议」）

用户贴了实时语音的完整对话记录，据此发现 A 档（全部转交）的三个具体缺陷，按建议先修 1+3 再修 2：

**1. 噪音也起 Agent 轮（最伤）** —— 记录里 `【语音指令】不如自己喝。`、`【语音指令】什么金？`
（都是误识别/旁音）各起了一整轮 Agent（19s / 7.2s，Agent 还得先猜用户想说什么，甚至去读
`project.godot`）。修法：`liveStreamControl.shouldDispatchVoiceTurn()` 三道**硬条件**，不猜意图
（关键词太脆、靠模型配合不可靠）：
- 去掉标点与语气词后什么都不剩 → 不转交（`嗯` / `啊？` / `。。。`），但「嗯，帮我改一下」照常转交，`改`/`停` 这类单字实义指令**不误杀**；
- 与上一条**同句**（忽略标点差异）→ 不重复转交（误识别常吐两遍）；
- 距上次转交 <1.2s 的连发 → 只转交一次（同一句被切成两段）。
被挡下的回合在状态栏明说（「「嗯」没有转交（像语气词或重复语音）」），**不静默吞掉**。

**2a. 框架说明泄漏成用户消息** —— 先前把「【语音指令】…（来自开发舱实时语音…）」拼进 prompt，
于是这段内部说明以**用户自己的话**的形式出现在对话流里。现在 prompt **只放用户原话**，框架走
新的 `ui_context=cockpit_voice_turn`（后端转成 system_context，不作为用户消息展示）。

**2b. 回答形态没约束** —— 记录里一句「你好」换来一整段能力菜单（「我可以帮你：看代码／查问题／
改代码…想从哪一项开始？」），而用户是**听**语音的，长菜单既没用又和语音回复重复。修法：后端为
`cockpit_voice_turn` 追加指引——最多两三句、口语化、不要罗列能力清单、不要复述指引、**闲聊时
不要提议改动项目**、只在明确要求改/修/建时才提方案并等审批、转写明显是旁音时问清即可不要猜着动手。
同时该上下文**照旧注入实时观察时间线**（「边看边聊」不丢），`chat.ts` 的 `visual_timeline` 条件同步放宽。

**验证**：新增 `tests/test_voice_turn_context.py`（3 条：框架进 system_context 而 question 就是用户
原话、语音轮次仍带时间线且标未核实、别的上下文不被套上语音约束）；`test_live_stream_control.py`
node 断言扩到门控的**两个方向**（该转的交、不该转的不交，含「语气词开头的正常句子」与「单字实义
指令」两个边界）；前端 `npm run build`（含 typecheck）通过；全量套件 **2337 passed / 6 skipped /
0 failed**。上下文里 `agent.py` 有别的 lane 的在飞改动，我只提交了自己这几个文件。

**本次未处理（如实说明）**：记录里还有两处属别的 lane —— ①英文思维链（`▾ 深度思考 The user's
voice input is…`）直接展示在语音回合里，语音场景不该展开推理，且不该是英文；②回合两次出现
「模型输出尚未结束，正在继续执行。」后没有下文（Agent 的收尾/流式）。另有 6：短句 STT 质量差
（`不如自己喝`/`什么金？`）属 R12 真机侧，它是第 1 条的放大器。

**顺带观察（别人的 lane，未动）**：全量套件曾出现 3 条 `test_mcp_inline_tools` 失败（单跑 31 passed，
该文件干净且非我改动），下一次全量跑即消失——又是一个混跑红，报因不明，留给对应 lane。

## 2026-09-30 修「幕布偏左」：docked 面板被入场动画的 transform 拖出屏幕（AI-F，用户指派）

- **现象（用户报）**：开发舱 → 引擎画面/场景，面板内容整体偏左，画面区大半在屏外，只剩右侧 Bug 列表飘在幕布中间。
- **定位手段**：装了 playwright，但它下载 chromium 的 CDN 在这台机器上**拿不到**（下了几分钟 0 字节），改用**系统已装的 Edge**（`channel="msedge"`）驱动，零下载。探针 `tests/manual/probe_layout.py` 会把候选容器的 `getBoundingClientRect` + computed style + 舞台子树「标签/类名/盒子」清单落盘到 `.tmp/layout.json`，并截图 `.tmp/shot_layout.png` —— 这类「看着不对但说不出哪里」的问题，量盒子比猜 CSS 快得多。
- **根因（不是居中问题，是层叠问题）**：`.pb-pop` 自己身上挂了入场动画，且 `fill-mode: both`：
  `@keyframes pb-pop-in { to { transform: translate(-50%,-50%) scale(1) } }`。
  **CSS 动画在层叠里压过普通声明**，所以 docked 形态 `.pb-pop-inline` 里的 `transform: none` **根本没生效**（而同一规则里的 `left/top: auto` 生效了）。面板按静态位置落位后又被 translate 拖走自身宽高的一半：实测 1993 宽 → `left = 39-996 = -957`，1057 高 → `top = 482-528 = -46`。两个数字与实测**逐位吻合**，坐实根因。
- **修法（一处声明）**：`.pb-pop-inline` 补 `animation: none;`（[SceneRuntimePanel.vue:1184](frontend/src/workbench/components/SceneRuntimePanel.vue#L1184)，并写明为什么必须显式关掉动画）。popup 形态不受影响，仍需那条入场动画。
- **验证（同一探针前后对比）**：

  | | 修前 | 修后 |
  |---|---|---|
  | `.pb-pop-inline` | `[-957, -46, 1993, 1057]` | `[39, 482, 1993, 1057]` = 与 `#wb-cockpit-runtime-slot` 完全重合 |
  | 画面框 `.pb-framewrap` | `[-945, …]`（大半在屏外） | `[51, 626, 1589, 903]` |
  | Bug 侧栏 `.pb-side` | `x=656`（飘在幕布中间） | `x=1652`（回到右列） |

  截图亦确认面板铺满幕布、左上工具栏恢复。前端 `npm run build`（含 typecheck）通过；`test_live_stream_control` / `test_live_audio_control` / `test_desktop_entry` / `test_live_vision_alerts` **17 passed**。
- **环境改动（告知）**：`.venv` 里装了 `playwright`（38 MB，pip 可通）；**chromium 本体没下**（CDN 被挡），走系统 Edge。想复用就 `python -B tests/manual/probe_layout.py [宽] [高]`。
- 归 AI-B lane 的文件（`SceneRuntimePanel.vue`），我只改了那一处声明。

## 2026-09-29 语音指令转交主 Agent（A 档：全部转交）+ 语音前端人设（AI-F，用户指派「A. 全部转交」）

- **起因（用户实测）**：对着开发舱说话说「你能帮我改吗？」，模型答「我没法直接帮你改，但我可以一步步告诉你怎么调整」。
- **根因两条，都不是 bug 而是从来没接**：
  1. **原生实时模型根本没有系统提示**：`.env.example` 预留了 `DOCMIND_OMNI_INSTRUCTIONS`，但 `.env` 没设，`_session_payload()` 只在非空时才带 `instructions` → 会话一条人设都没有，模型按厂商默认助手人格答话。它那句「没法帮你改」是**字面实话**——它不知道自己是谁、也不知道背后有个能改文件的 Agent。
  2. **语音转写从来没有转交主 Agent**：前端对 `audio.transcript` 的全部消费只有「合并进字幕 + 刷新相位」，没有任何一处把语音指令派给主 Agent。（R7 的「用户纠正即转交」、画布目标的「边看边聊」此前只做了一半：能聊，不能转。）
  ⚠️ **技术前提**：实时模型**没有工具通道**（网关只转发音视频/文本事件），它自己永远改不了文件——「转交」必须在我们这侧做。
- **用户选定的档位**：A（**全部转交**），代价是闲聊也会触发一轮 Agent，已被告知。
- **实现**：
  - `agent_runtime/realtime_omni.py`：新增 `DEFAULT_INSTRUCTIONS`（出厂人设：你是开发舱的**语音前端**、回答会被朗读所以要短、用户每句话都会被同时转交开发 Agent 所以**永远不要说「我没法修改」**、不要承诺实现或编造结果）。`DOCMIND_OMNI_INSTRUCTIONS` 仍可覆盖；`.env.example` 注释同步说明「留空则用内置人设」。
  - `frontend/src/workbench/components/AutonomousCockpit.vue`：用户转写 final 收尾后 `sendVoiceTurnToAgent()` → `appEvents.emit('docmind:send-chat')`（**不传 uiContext**，让对话台按既有规则自动选 `cockpit_live_vision` 并附上当前画面与实时观察时间线）。对话台忙（有待审的审批门）时不会自动重试，所以把 `pending` 明写到状态栏，用户能看到这条语音指令没送进去。
  - `frontend/src/workbench/liveStreamControl.ts`：新增纯函数 `closeUserTranscript(captions, text, hadDraft, limit)`，**返回值 `null` 表示「这条 final 是重放」**——调用方据此同时不上字幕、不转交。转交与记账绑在一条线性路径上，就不可能出现「字幕去了重、语音指令却重复派给主 Agent」或反过来漏转。
- **验证**：`tests/test_realtime_provider.py` 38 passed（新增 2 条：默认人设必须下发、显式 `DOCMIND_OMNI_INSTRUCTIONS` 必须覆盖默认）；`tests/test_live_stream_control.py` node 断言扩到含「重放返回 null / 有草稿整条替换 / 空 final 不记账 / 窗口有界」+ 接线断言（含 `closeUserTranscript` 与 `if (!next) return` 必须成对出现）；**反向验证承重**：关掉同文去重守卫 → node 断言立刻红。前端 `npm run typecheck` + `npm run build` 通过；全量套件 **2221 passed / 6 skipped / 0 failed（272s）**。
- **顺手纠正自己**：我先写的那条「转交必须排在收尾分支之后」的源码断言是**假守卫**（分支条件文本本身在调用之前，插进分支里也照样通过），已删掉，改成「纯函数的行为断言 + 接线成对断言」——能真失败。
- **仍待办**：①后端要**重启**才生效（截图里 `Error append image before append audio.` 还在，说明 23:41 的构建已生效但 uvicorn 仍是旧进程）；②档2（`item_id`）价值下降——R13 lane 已在 `appendLiveCaption` 里加了「同文 final 去重」，覆盖了重放重复的常见情形，剩下的只是「中间隔了 AI 回复的重放」，要根治仍需 `item_id`（厂商是否下发该字段目前**无据可查**，需要一次真机探针）。

## 2026-09-29 媒体顺序闸门（首帧前先补音频）+ 修掉本文件长期的收尾 flake（AI-F，用户指派「接」）

### 一、媒体顺序：首帧之前必须先有音频

- **问题（AI-G 设备侧实测）**：真实 `dashscope_omni` 在同一会话里要求**先音频后图像**——先送帧会回
  `vendor_error: Error append image before append audio.`；先送一个音频块即可解除。而 UI 的自然
  顺序恰恰是「先开摄像头/屏幕共享 → 再点开麦对话」，**生产路径会稳定踩中它**。
- **修法**：`agent_runtime/realtime_omni.py` 的 `send_frame()` 加单一闸门——本会话还没送过音频时，
  先补一个 100ms 的 16kHz PCM16 静音块再送帧。**不改协议、不改前端顺序、只此一处**。
  - 走 `send_audio()` 而不是自己拼事件：这样它也进重放缓冲（重连后顺序依然成立），且
    `_audio_primed` 由同一条路径置位。
  - `_audio_primed` 只在发送**成功**后置位：送失败就保持未置位，下一帧自动重试。
  - `close()` 里清掉该标记：重连（`recover()` = close + start）是**全新会话**，供应商的顺序约束
    按会话计，不清就会在重连后再次踩坑。
  - 顺手把静音块算法抽成 `_silence_chunk(step_ms)`，与 `send_silence_tail()` 共用，避免两处漂移。
- **验证**：`tests/test_realtime_provider.py` 新增 4 条 + **36 passed**；**反向验证过承重**——把闸门
  关掉，新用例红 3 条（第 4 条是「音频先行时不得再塞静音」的守卫，两向都绿，符合设计）。
- **如实说明（未验证的窗口）**：AI-G 的对照实验是在**已建立的会话**里做的；而本闸门发生在首帧，
  理论上可能早于供应商的 `session.created`。若真机上出现「首帧时 `vendor_error`」，下一步是把首帧
  也闸在 provider ready 之后（需要在 provider 里跟踪 `session.created`），而不是继续加静音。

### 二、修掉本文件长期间歇红的收尾 flake（根因不是预算，是夹具把事件循环拆了）

- **现象**：`test_session_close_releases_provider_timeline_and_metric_scope` 5 次跑里红 1~3 次；把它
  的等待从 5s 加到 **20s 也照样红**——说明不是在等「慢」，是在等一个**永远不会来的**东西。
- **根因**：`gateway` 夹具用 `with TestClient(api.app)`，退出时会把该 client 的**事件循环拆掉**；
  而会话拆除跑在服务端 handler 的异步 `finally` 里、比客户端 socket 退出还晚。循环一拆，那个
  `finally` 就再也不会完成 → 时间线/指标作用域永不回收 → 断言只剩「循环拆掉之前它恰好跑完了」
  这一种运气。对照：R9 的 `_gateway_client()` 从不调 `client.__exit__()`，所以他们那条同样结构的
  用例是稳的——差别只在循环什么时候死。
- **修法（两处，都不是放宽断言）**：
  1. `gateway` 夹具在**退出 client 之前**等收尾跑完（`_wait_for(没有活跃时间线, 20s)`）——同步点放
     在循环还活着的时候，跨用例隔离也随之确定；
  2. 把「收尾三件事（关 provider / 释放时间线 / 回收指标作用域）」这条属性**挪到确定性层面**：
     直接驱动 `SessionBridge.close()`（同步、次序在源码里就是 provider.close → release_timeline →
     drop_session），不再经 WebSocket 去赌服务端 finally。
- **验证**：该文件**连跑 5 次全 27 passed**，且是在**负载重得多**的条件下（单次 93~153s，原先
  通过时 ~23s、失败时 ~17s）；全量套件 **2129 passed / 6 skipped / 0 failed（296s）**——这是本文件
  在重负载下第一次全绿。
- 不删用例、不降断言强度：属性仍被钉住，只是换到了能被确定性观察的层面（R9 那条 close 抛错的情形
  仍由 `tests/test_realtime_resource_security.py` 覆盖）。

## 2026-09-29 R12 真实设备联验（AI-G / 当前设备侧核对）

- **判定：设备侧未通过，不能伪造真机完成。** 本机 Codex In-app Browser 能打开工作台并进入 `godot_sample` 开发舱，但没有可供授权的独立摄像头/麦克风设备；“打开摄像头”和“共享屏幕给 AI”未取得可持续媒体流，无法验证真实采集、扬声器播放、连续多轮或抢话听感。
- **原生握手证据**：本地 `uvicorn` + `prj-3da07fde89a5` 的 `/api/vision/live-stream` 收到 `hello.ok`，模式为 `native-realtime`，provider 为 `dashscope_omni`，能力含 `audio.in/video.in/text.out/audio.out/interrupt`。
- **新增关键阻塞（需前端/网关 lane 认领）**：对真实 `dashscope_omni` 的合成媒体对照表明，先送视频帧再送音频会返回 `vendor_error: Error append image before append audio.`；先送 1 个音频块再送视频帧未复现该错误。当前 UI 可先开摄像头/屏幕共享、再点击“开麦对话”，因此生产路径可能踩中顺序限制。设备复验前应加入音频就绪闸门，或确认 provider 支持视频先行。
- **报告**：`docs/realtime-r12-device-acceptance-20260929.md`，含浏览器状态、原生握手、媒体顺序回包和逐项设备状态；真实设备联验仍需在有摄像头/麦克风/扬声器的 Windows Edge/Chrome 上重跑。
- **范围边界**：本节不修改、不重复实现 R12 provider 可靠性三条（静音尾、`stream_broken` 恢复、`done` 严格成功判定）；代码/离线证据见下方既有 R12 provider 节。

## 2026-09-29 R2 语音闭环前端（liveAudioControl + cockpit 麦克风/播放接线）【AI-A/AI-B，本轮局部提交】

- **定位**：把豆包式"边说边聊"缺的语音上行/下行/自动抢话缝起来。纯数学与状态机全部放**新模块 `frontend/src/workbench/liveAudioControl.ts`**（零浏览器依赖，可脱离页面跑）；`AutonomousCockpit.vue` 只做 getUserMedia/AudioWorklet/AudioContext 接线。未动 `realtimeProtocol.ts`、网关、`voice_dialogue.py`。
- **契约来源（必须遵守，非我发明）**：AI-D 真机结论——服务端 VAD 靠 **~1s 尾部静音**收句，"停止说话 ≠ 停止推流"（HANDOFF 0db20ff / /root 已在 provider 侧加 `send_silence_tail` 双保险，前端这层是第一条防线）；输入 pcm16@16k、输出线报 `encoding:"pcm24"` **采样率未明**——播放按 pcm16@24k 单点假设 `OUTPUT_SAMPLE_RATE`，界面限制已如实标注，真机噪声只改这一个常量（R12）。
- **模块行为**（`tests/test_live_audio_control.py`，node 真跑 34 断言）：`resample/floatToInt16Le/frameRms` 采集数学（LE 字节序逐字节钉死）；`createVoiceGate` 能量 VAD idle→speech→tail→idle：起说 2 帧去抖、300ms 迟滞判说完、**tail 全程 sending=True**、推满 1s 才 tailDone；抢话（AI 在说时起说）报 `bargeIn`；`createChunkPump` 只产 100ms 整片不吐碎片，tail 期喂等长零样本按实时节奏自然产静音尾；`encodeAudioPacket` 沿用 R0 媒体包头，负序号/零载荷/非整 captured_at 全拒。
- **cockpit 接线**：`开麦对话` 按钮（流连接后可用）→ worklet tap（零增益回授防护）→ VAD→分片→**复用现有 live-stream WebSocket** 上行 `audio.chunk`；`model.audio` base64→Int16@24k 排队播放 + `liveAiSpeaking` 状态；barging 时**自动停播 + 自动发 `realtimeCancel`**；`stopLiveVision`/卸载路径全量复位；打断按钮同步停播。面板显示"AI 正在说话，可直接开口打断"。
- **验证**：`tests/test_live_audio_control.py` 3 passed + `test_live_stream_control.py` 3 passed（含新增音频接线回归）；混跑 `-k "realtime or live_vision or live_stream or live_audio or voice"` = **277 passed / 0 failed**；前端 `npm run typecheck`/`build` 通过。调试坑两处：raw 字符串里 `""" \` 续行符会成字面量把 harness 首行炸掉；测试断言 VAD 时序必须"喂到信号出现"而不是硬编码帧序。
- **未验证（不谎报）**：麦克风采集与扬声器播放是浏览器 API，本机无设备环境——**上行听感、播放音质（含 24k/位深假设）、真实 barge-in 手感全部属 R12 真机**；上行依赖原生模式（`hello.ok.mode=native`），抽帧模式下开麦会收 `audio_not_ready`（协议既有行为，正确）。
- **冲突面**：`AutonomousCockpit.vue` 是多人交叠文件，本次提交为整文件（含其他 AI 已在工作区的前端改动），如归属有异议以 diff 中 `liveAudioControl/startLiveMic/playLiveModelAudio/acp-live-audio` 相关块为本轮新增。

## 2026-09-29 R12 provider 可靠性三条收口（/root，已完成代码·勿重复）

- **范围**：本轮只处理 R12 指定的 provider/网关/验收 runner 可靠性契约；AI-G 仍负责真实摄像头、麦克风、屏幕共享和驾驶舱 UX 联验。不要重复修改这三条。
- **停止说话后的尾部**：`agent_runtime/realtime_omni.py` 新增 `send_silence_tail()`，默认继续发送约 1 秒、每块 100ms 的 PCM16 零音频，让服务端 VAD 正常收句。
- **`stream_broken` 恢复**：Omni provider 保存有界的未完成音频；`recover()` 会重建连接、等待 ready 并重放音频。`api.py` 将 `retryable=true` 的 `stream_broken` 变为后台恢复，恢复失败才结束 WebSocket，不把它作为致命错误直接上抛。
- **成功判定**：`verify_realtime_acceptance.py` 只有收到 `done` 且有 assistant 文本或音频才算成功；仅有 `transcript`、没有 `done` 的回合会失败并重连重放。旧 fake provider 没有静音尾接口时仍走兼容 fallback。
- **验证**：`tests/test_realtime_acceptance.py`、`tests/test_realtime_provider.py`、`tests/test_realtime_gateway_bridge.py`、`tests/test_realtime_resource_security.py` 共 68 项；排除既有收尾竞态用例后 **67 passed**，该收尾用例单项复跑通过。整组联跑出现 1 个既有 `test_session_close_releases_provider_timeline_and_metric_scope` 时序 flake（67 passed / 1 failed），需后续单独复跑，不归因于这三条逻辑。
- **后续**：真实模型/设备验收报告需要重新运行以刷新证据；本节代码已完成，AI-G 继续设备侧联验，其他 AI 不要重复实现静音尾、重连重放或 `done` 判定。

## 2026-09-29 ⚠ R7 其实已经实现，AI-C 勿从零开工（AI-F 补查，供 AI-C / R14 核对）

- 起因：用户问「R7 是谁做的」。表上 R7 仍写「AI-C，0%」，但树里已有完整实现。
- **产物**：`voice_dialogue.py`（低延迟陪聊：读主 Agent 最新结果、无工具无写权限、明确把要执行的事项转交主 Agent）+ `api.py:3169` 的 `POST /api/voice/dialogue` + `tests/test_voice.py` 的两处用例（模块级 `voice_dialogue.reply` 与端点 `test_voice_status_and_dialogue_api`）。
- **归属证据（指向 /root 的「语音协作」线，与 R2 同一条线）**：`045364b`（提交信息自带 "root, 语音协作 line"）只提交了 `voice.py` + `tests/test_voice.py`，但那份测试**当时就已 `import voice_dialogue`** 并测 `/api/voice/dialogue`；而 `voice_dialogue.py` 与那个端点当时都**不在任何提交里**，直到收口提交 `503e27e` 才第一次进历史（同批还有 `/api/voice/{status,transcribe,speech}`）。先写测试、后补模块，是同一人所为的典型形态。
- **局限（如实说明）**：未跟踪文件无法从 git 证明作者，以上是依据「测试归属 + 提交信息 + 端点分组」推断；如与事实不符请当事人更正。我没有改 `voice_dialogue.py` 一个字符。
- **给 AI-C 的建议**：别按表从零开工 R7（会重演 R2 的重复造轮子）。改做 R7 的收尾/加固：网关 `audio.chunk` 通道开放后的真机联验（R3 目前仍回 `audio_not_ready`）、未覆盖分支补齐。
- 公告表 AI-C 行的证据与下一步两格已按此更新（只补证据与建议，未改其阶段/进度归属）。

## 2026-09-29 R12 provider 侧联验（AI-D：范围声明 + 一项根因，勿与 AI-G 重复）

- **范围声明**：R12 在任务表里归属 AI-G（真实设备采集 + 驾驶舱体验调优）。本轮只交付 **R4 provider 侧**证据，未做摄像头/麦克风/屏幕共享采集，未改驾驶舱 UI。AI-G 接手时不必重做 provider 联验，请直接从设备侧接。
- 产出：`verify_realtime_acceptance.py` → `docs/realtime-r12-acceptance-20260929.md`。真 Key + 真模型 `qwen3.8-omni-flash-realtime`（音色 `Jennifer`）。五项指标：首响应 3171 ms（含 3 s 推流与 VAD 等待）、模型延迟 223 ms、持续响应 3/3 轮、打断后新增 0 条、断线重连成功、错误率 0.0%（0/75）。
- **根因（R3 网关与 R12 设备侧都得遵守）**：语音结束后**必须继续推约 1 秒静音**。服务端 VAD 靠尾部静音收句；在最后一片采样仍响亮时掐断流，turn 就一直不闭合，服务端约 8.5 s 后回收会话——现象是「转写完整（8–19 条 transcript）却零回复 + `stream_broken`」。对照实验（同片段、全新会话）：不补静音 0/2 成功，补 1.0 s 静音 2/2 成功。此前 turn-3 的失败曾被我记成「与音频内容相关」，真因是片段尾部能量高被硬截断，**不是模型不稳定、也不是账号问题**。
- **`commit()` 不能当这个坑的兜底**：VAD 已收句后提交空 buffer 会回 `vendor_error`；`commit()` 只用于客户端 VAD 模式。
- 同步修复：`verify_realtime_live.py` 也补了静音尾（`--tail`，默认 1.0 s），`agent_runtime/realtime_omni.py` docstring 记录该契约。
- 给网关的三条硬要求（报告末尾同款）：① 停止说话 ≠ 停止推流；② `stream_broken` 不得当致命错误上抛，要内建重连重发；③ 成功判定看 `done`，有 `transcript` 无 `done` 视为失败。
- 测试：`tests/test_realtime_provider.py` 31 项全绿。

## 2026-09-29 R9 收口：实时资源/韧性/隔离只读审计（AI-A/AI-B，已完成·勿重复）

- **审计范围**：只读测试 `tests/test_realtime_resource_security.py`，不修改 P0/P1 的 enterprise_sandbox、process_runner、命令预算或确认门实现。
- **覆盖证据**：provider `close()` 外抛仍释放时间线和会话指标；超限音频在入口拒绝且不转发；原生 provider 拒收帧时回落抽帧槽；项目 B 状态不泄漏项目 A 时间线；`interrupt()` 外抛时 `cancel.ok` 仍先返回且会话存活。
- **验证**：`tests/test_realtime_resource_security.py` **5 passed**；与 R9 守卫实现和 bridge 回归边界一致。
- **重复防护**：任务表已把 R9 标记为“审计完成·勿重复”。后续只在 R12 真实设备或资源压测发现具体缺陷时追加证据，不重复实现权限/沙箱逻辑。

## 2026-09-29 R9 审计：实时链路资源/韧性/隔离测试（AI-A/AI-B 兼任）【局部提交】

- 领取 R9 的只读测试部分（用户确认）。**只新增 `tests/test_realtime_resource_security.py`（5 项）**，与 AI-F 的 `test_realtime_gateway_bridge.py` 互补不重复，未改任何实现。
- 覆盖：`provider.close()` 外抛时收尾链仍完整（时间线释放+作用域回收）；原生模式超限音频在入口被拒、**不转发进 provider**；`send_frame` 返回 False 时帧**回落抽帧单槽**不静默丢；跨项目 `status_snapshot` 不泄露他项目时间线条目；守卫回归（见下）。
- **⚠ 审计指出的真实缺口 → 已闭合**：网关 receive 循环的 `bridge.interrupt()/send_frame()/take_audio()` 三处原本**没有 try 守卫**（而 `start()`/pump 有），provider 违约外抛会炸穿整条 WebSocket（实测抢话 cancel 直接击穿、客户端收到服务端异常）。已上报 /root，**当日落地于 commit 6832251**：interrupt 外抛→本地取消照旧回 cancel.ok 并补发 `interrupt_failed` 错误；take_audio 外抛→回落 audio_not_ready；send_frame 外抛→仅降级该帧不翻转会话模式；三处均记 `note_model_failure()`。本文件第 5 条按守卫后语义钉回归（cancel.ok 先到、interrupt_failed 随后、心跳证明会话存活）。
- 调试记录（后人少踩）：首版含「8 轮快速开关不泄漏」用例，连败两次——共享 project 引用计数+迟到 release 的竞态（AI-F 已记录同款坑）；改独立 project 后仍依赖异步收尾时序，**与 AI-F 标注 green/red 交替的 `test_session_close_*` 同源，遂删除该重复面**——共享仓库里间歇红的测试比没有测试更糟。首版 flaky 版被收口提交 `e35ce8e` 卷入过 HEAD，本次提交即为修正。
- 验证：本文件 5 passed 连跑两遍稳定（~2.4s）；混跑 `-k "realtime or live_vision or live_stream or voice"` = **270 passed / 0 failed**。
- 未覆盖（如实说明）：服务重启恢复（bridge/timeline 为纯内存设计，重启即清零属预期，无媒体落盘可验）；GPU/CPU 限制不在实时链路内（归本地模型线）。R12 真机下这两项需复核。

## 2026-09-29 修 R9 缺口：网关三处 provider 转发调用加守卫（AI-F，用户指派「修」）

- **缺口（R9 只读审计报出，见本文件 R9 节）**：`api.py` 的 cancel / 音频 / 视频分支直接调
  `bridge.interrupt()`、`bridge.take_audio()`、`bridge.send_frame()`，**一个 try 都没有**；而
  `start()` 与 pump 循环都有。provider 基类契约写明「fail-closed，返回 False 而非外抛」，但契约
  不等于保证——适配器违约一次，外抛就顺着外层 try（只接 WebSocketDisconnect）打穿整条会话。
- **修法（三处各按语义兜底，都不动成功路径）**：`interrupt` 外抛 → 本地取消照常完成，另发一条
  `interrupt_failed` 如实说明模型没停住；`take_audio` 外抛 → 按「没接管」落回既有
  `audio_not_ready` 降级回复；`send_frame` 外抛 → **只降级这一帧**到抽帧路径，不中途翻转整场会话
  的模式（模式已在 `hello.ok` 里承诺过）。三处都记 `note_model_failure()`。
- **次序是契约的一部分（踩过）**：错误事件必须发在 `cancel.ok` **之后**。先发错误会让
  「读第一条回包」的断言静默变绿（错误恰好不是 cancel.ok），缺口关闭了却看不出来；调过来后
  R9 那条 known-gap 测试会按作者预写的方式响亮报错。
- **R9 测试已按作者预写的说明翻转**：`test_known_gap_interrupt_is_not_guarded_and_kills_the_session`
  → `test_guarded_cancel_survives_a_provider_that_raises`（断言 cancel.ok 先返回、随后 error、
  心跳往返证明会话存活）。该文件当前版本同时删掉了那条负载敏感的 rapid-cycles 用例（作者所为）。
  它必须与次序调整**同批**入库，否则 HEAD 上「旧断言 + 新次序」是红的——所以它在 `d923445` 里
  与我的改动一起提交，其余内容仍属其作者 lane。
- **验证**：`tests/test_realtime_gateway_bridge.py` **27 passed**（新增 3 条分别覆盖三处）；
  **并反向验证过承重**——去掉守卫跑 `503e27e` 版 api.py，3 条全红。R9 文件 5 passed，
  两文件合跑 32 passed；全量套件 **2117 passed / 6 skipped / 0 failed**（186s）。
- 提交：`6832251`（三处守卫）+ `d923445`（取消答复次序 + 翻转 R9 测试）。

### ⚠ 两条通报（不是我的改动，请相关 lane 认领）

1. **本文件下方「补全桌面复验前端链路（AI-F）」一节署名 AI-F，但不是本会话所做**——我这个会话
   没有碰过任何前端文件（本会话的改动只有：`agent_runtime/process_runner.py`、
   `agent_runtime/realtime_bridge.py`、`api.py`、三个测试文件、HANDOFF）。代码本身是完整且正确
   的（链路逐段核对过：Cockpit `dispatchFeedback` → `docmind:send-chat` → ChatDock `onSendChat`
   → `send(reviewRef)` → `askGrounded` 的 FormData → 后端对账），但 `chat.ts` 与 `ChatDock.vue`
   的这两处改动**目前仍未提交**（mtime 20:02，在我那次前端收口提交之后）。署名请核一下，
   以免交接时误判谁在改哪个文件。
2. 原先记的「前端不转发 workflowId/feedbackId」缺口**已关闭**（同上，由那条链路补齐），
   所以 `/api/chat` 的桌面复验分支不再只是测试在调。

## 2026-09-29 补全桌面复验前端链路（AI-F）

- 缺口：后端 `/api/chat` 在 `ui_context=desktop_visual_review` 时强制 `workflow_id`+
  `feedback_id` 对账，但前端只有 Cockpit 产出这两个编号，`chat.ts` 的 `askGrounded` 不收、
  FormData 不发，该分支此前只有测试在调。按真实链路逐段补齐 3 处：
  - `frontend/src/workbench/api/chat.ts`：`askGrounded` opts 增 `workflowId`/`feedbackId`，
    仅在 `uiContext==='desktop_visual_review'` 时 append 进 FormData（字段名对齐后端
    `Form("workflow_id")`/`Form("feedback_id")`）；
  - `frontend/src/workbench/components/ChatDock.vue`：`send()` 末参增
    `reviewRef?: { workflowId?; feedbackId? }`，透传到 askGrounded opts；
  - 同文件 `onSendChat`（`docmind:send-chat` 消费方）：`detail.uiContext` 为桌面复验时，
    把 `detail.workflowId`/`detail.feedbackId` 作为 reviewRef 传入 send。
- 数据流闭环：Cockpit `dispatchFeedback`（AutonomousCockpit.vue:1454-1457，编号取当前
  workflow + record.id）→ `appEvents.emit('docmind:send-chat')` → ChatDock `onSendChat` →
  `send(reviewRef)` → `askGrounded` FormData → 后端对账。
- 边界：ask 页 `AskApp.vue` 的 send 不产生桌面复验反馈（该场景只在运行台 Cockpit，固定走
  ChatDock），未改动。
- 验证：`npm run typecheck` → 0 错误；`npm run build` → ✓ built in 7.71s；编辑器诊断 0。
  后端契约由 `test_desktop_review_chat_requires_current_workflow_feedback` 锁定，全量 2109 passed。

## 2026-09-29 R0/R3 收口：实时信封字段完整保护（/root，已完成·勿重复）

- **修复**：实时协议与 provider 事件序列化现在统一保护完整信封字段 `v/type/sent_at/sequence/captured_at/session_id`；不可信 provider payload 不能覆盖网关生成的会话、序号或采集时间。
- **范围**：仅修改 `agent_runtime/realtime_protocol.py`、`agent_runtime/realtime_provider.py` 的序列化边界和 R0 契约测试；没有新增事件类型、没有改变 R3 网关路由或 R4/R5/R10 接线。
- **验证**：`tests/test_realtime_protocol_contract.py tests/test_realtime_gateway_faults.py tests/test_realtime_gateway_stress.py` **105 passed**；`py_compile` 与 `git diff --check` 通过。
- **重复防护**：R0/R3 收口已完成，后续不要在协议或网关重复添加第二套信封过滤逻辑；真实设备联验仍属于 R12。

## 2026-09-29 visual_targeting 7 漂移收尾 + 编码回退真实修复（AI-F，接续同文件下一节的工作）

- 用户指派处理 7 个 visual_targeting 失败。下一节记录的端点补齐工作（并发 AI-F 会话）在我
  run3 进行期间落盘，我接手时实现已在位，经我逐行审读（提案 TTL/单次 used、三闸门、
  同窗口复验、/api/chat 反馈校验）确认契约完整，实测 `pytest tests/test_visual_targeting.py`
  → **11 passed**（开始前 7 failed）。
- **但下一节声称的「2108 全绿」在本环境不成立**：我随后的全量 run4 出现 1 个确定性红点
  `test_command_execution.py::test_encoding_decision_survives_a_split_multibyte_char`
  （单跑也红，非 flake）。根因：本工具宿主给子进程注入了 **`PYTHONUTF8=1`**（非用户/机器
  持久变量，仅本宿主进程树），UTF-8 模式下 `locale.getpreferredencoding(False)` 恒报 utf-8——
  `_StreamDecoder` 判定「不是 UTF-8」后回退编码仍是 utf-8，GBK 字节全成替换符（实测
  gbk 四种分块全红，`decided encoding: utf-8`）。
- **我的修复**（`agent_runtime/process_runner.py`，未跟踪文件）：新增
  `_windows_legacy_encoding()`，用 `kernel32.GetACP()` 取系统真实 ANSI 代码页（本机 cp936，
  codecs 归一为 gbk），UTF-8 模式不受影响；Win32 不可用才退回 locale 口径。修复后
  utf-8/gbk × 1/2/3/8192 八种切法全过。
- 最终证据（真实执行，均在本宿主 PYTHONUTF8=1 环境下）：
  - `pytest tests/test_command_execution.py tests/test_visual_targeting.py` → **44 passed**；
  - 全量 `pytest tests -q --basetemp=D:\Temp\pytest-docmind-bt3` →
    **2109 passed, 6 skipped, 0 failed, 0 errors in 182.65s**；
  - 前端 `npm run typecheck` → 0 错误（本轮未改前端）。
- 下一节第 2 节末尾记的前端缺口（chat.ts 不转发 workflow_id/feedback_id）仍开放，属
  AI-B/ChatComposer lane，本轮未动。

## 2026-09-29 三项修复：命令输出编码 / 视觉点击端点 / EVENT_DONE 缺口（AI-F，用户指派「修复」）

用户就「修复」明确了范围：**只修编码 bug、8 个失败测试、EVENT_DONE 缺口**。三项全部完成，
**全量套件首次全绿**：`pytest tests -q --basetemp=D:\Temp\pytest-docmind-bt` →
**2108 passed, 6 skipped, 0 failed**（164s，3 分 22 秒前一次为 1 failed / 2104 passed，见下「顺手发现」）。

### 1. 命令输出编码：按流判定，不再假定本地编码（`agent_runtime/process_runner.py`）

- **根因**：读侧用 `locale.getpreferredencoding(False)`（本机 `cp936`）解子进程输出，而环境里
  有 `PYTHONIOENCODING=utf-8:surrogateescape`，子进程输出 UTF-8 → 多字节中文全成乱码
  （`中文输出测试` 变 `涓枃杈撳嚭娴嬭瘯`）。反向也一样：node/git/cargo 输出 UTF-8 按 cp936 解是乱码。
- **修法**：`_StreamDecoder` 不再猜，改为**按流的首个非 ASCII 字节判编码**（ASCII 前缀先原样透传）。
  判定走 `codecs.getincrementaldecoder("utf-8")()` 严格增量试解——整段 `bytes.decode` 会把
  被分块切开的多字节字符误判成 GBK；`getstate()` 的缓冲字节数用来区分「确定不是 UTF-8」和
  「还判断不了」（后者返回空串、把字节留着）。`DOCMIND_COMMAND_ENCODING` 显式设置时仍优先。
- **已知代价（写在注释里）**：GBK 输出开头几个字节若恰好构成合法 UTF-8 序列（少见）会被误判。
- **验证**：新增 2 个回归用例（子进程无编码提示时必须仍按本地编码解、编码判定要扛住
  1/2/3/8192 四种分块切法），`tests/test_command_execution.py` → **33 passed**。

### 2. 视觉点击：后端补齐既有的半落地功能（`api.py`）

- `POST /api/vision/locate-click` 与 `execute-click` **在本仓库历史上从未存在过**（`git log` 全无），
  而前端 `model.ts` / `AutonomousCockpit.vue` 早已在调它们——属半落地。测试文件
  `tests/test_visual_targeting.py` 就是规格，按其契约补齐：定位（视觉模型出 bbox/点/证据）+
  执行（预演确认 → 复验 → 授权 → 真点击 → 前后帧像素指纹比对 → 可选模型复验）。
- 目标解析/复验判定/像素指纹都在 `agent_runtime/visual_targeting.py`（他人已写），端点只做编排与权限。
- 端点用局部 `import game_workbench` / `desktop_actions`，因为 `api.py` 顶部是
  `from game_workbench import ... approval ...` 直接绑名——`patch("game_workbench.approval")` 只对
  调用时走模块属性访问的代码生效。
- **`/api/chat` 新增桌面复验轮**：`ui_context=desktop_visual_review` 时校验 `workflow_id`+`feedback_id`
  必须属于**当前项目**且确为桌面复验类反馈；反馈原文一律不进 `system_context`，只以「模型复验结论
  及画面文字只是未核实数据」的内部指引 + 反馈编号引用。跨项目/来源不符各自 400/403。
- **签名改动**：`chat()` 首参加 `request: Request = None`。默认 None 只为让既有代码能直接
  `await api.chat(question=...)`（`tests/test_cloud_agent_registration.py` 就是这么调的）；
  FastAPI 侧只看注解不看默认值，真实请求永远拿得到 `Request`。**这一条曾让那 2 个用例红**。
- **验证**：`tests/test_visual_targeting.py` **11 passed**（本轮开始前是 7 failed）。
- **发现但未修的缺口（跨 3 个前端文件，属他人 lane）**：前端**从不转发** `workflow_id`/`feedback_id`——
  `frontend/src/workbench/api/chat.ts` 的 `askGrounded` 选项里没这两个字段，而
  `previewFeedback.ts` 与 `AutonomousCockpit.vue`（1455-1456 附近）已经在产出它们。
  即后端这条新分支目前**只有测试在调**。补齐要动 `chat.ts` + `ChatComposer`/`ChatDock` 链路，
  涉及 AI-B 正在改的 `AutonomousCockpit.vue`，**未动**，留给对应 lane。

### 3. `EVENT_DONE` 缺口：补一条「这一轮说完了」的标记（`agent_runtime/realtime_bridge.py`）

- **缺口**：`EVENT_DONE` 被 `WIRE_BY_KIND` 映射成 `session.closed`，转发就是撒谎（会话还活着），
  所以此前直接在 `next_events` 丢掉。但**丢掉也有代价**：一轮回答结束的信号本来由
  `model.delta` 的 `final: true` 承载，而那条**只在有转写时才发**——用户抢话打断、本轮只有音频、
  本轮输出为空这三种情况下没有它，前端那条 `done: false` 的助手回合就永远不收口，界面停在「回答中」。
- **修法**：`EVENT_DONE` 被拦下时改发一条结束标记——`model.delta` + `final: true` + **空文本**
  （`SessionBridge._end_of_turn`）。**不新增事件类型**（R0 的 `SERVER_TYPES` 一行未动，也没让前端改一行）：
  AI-B 的 `liveStreamControl.ts::appendCaptionTurn` 对「`final` + 空文本」的处理恰好是
  「当前开着助手回合就地收口，没开着就什么都不做」，天然幂等，不会造出空气泡。
  本轮已由 `final: true` 收过口的不再补发（`_turn_closed` 是**实例状态**：结束标记与被拦下的
  `EVENT_DONE` 可能落在两次 poll 里，用局部变量会把同一轮收口两次）。
- **验证**：新增 3 个用例（无转写轮必须补、已收口轮不得重复补、每轮各自补），
  `tests/test_realtime_gateway_bridge.py` → **24 passed**。并**反向验证过用例是承重的**：
  临时把补发关掉 → 2 个用例红（第 2 个是防重复的守卫，两向都绿，符合设计）。

### 顺手发现：全量套件里两个**顺序/负载敏感的存量 flake**（都不是功能缺陷，均未修）

- `tests/test_web_fetch.py::test_ddg_success_can_merge_parallel_backends`：该用例只 patch 了
  ddg/bing，**baidu 是真跑**，机器一忙就赶不上并行截止时间，于是 `web_search` 追加
  「（限时返回已有候选；未完成来源：baidu…）」→ 断言全等失败。单独跑 12 passed。
  该后缀在 **HEAD 里就有**（`git show HEAD:tools.py` 命中的是同一行），与 `tools.py` 那 725 行
  未提交改动无关（`git diff -U0` 未触及并行合并与截止时间那段）。
- `tests/test_realtime_gateway_bridge.py::test_session_close_releases_provider_timeline_and_metric_scope`：
  断言的是「客户端 socket 退出后，服务端处理器的 `finally` 异步收尾」——本文件文档里写明的那个
  时序差。机器忙时 5s 的 `_wait_for` 预算不够（失败那几次整轮 17.6s = 5s + 夹具 10s，成功时 2.1s）。
  **已用实验排除与本轮改动的关系**：把第 3 项的补发逻辑临时关掉，该用例**照样红**（3 次里红 1 次）；
  随后连跑 5 次全绿，两次整文件 24/24 全绿。
- **给后续 AI 的提示**：这两条属「单跑绿、混跑红」，不要为了让它们变绿去改断言语义；
  要动就动预算或把未 patch 的后端 patch 掉。

### 提交状态：**已按用户指令做全量收口提交（5 个 commit）**

用户确认走「全量收口提交」（工作树里 138 个路径，大部分是其他 lane 的累积改动）。按目录分 5 批入库：

| commit | 范围 |
|---|---|
| `503e27e` | 后端与运行时：`agent_runtime/**`、`api.py`、其余后端模块、`web/` |
| `c19aacf` | 前端工作台：`frontend/**` |
| `e35ce8e` | 测试套件：`tests/**`（含新增 `conftest.py`） |
| `5f1209c` | 文档：`HANDOFF.md`、`docs/**` |
| `326da8e` | 补遗：`realtime_bench.py`、`verify_realtime_acceptance.py` |

**没有**按特性拆分的原因写在 commit message 里：多数改动落在同一文件（`api.py` 未提交部分 1319 行，
我只占约 220 行），不做 hunk 拆分就无法按特性切。

**刻意未纳入**：`.tmp/`（诊断脚本与临时输出）；`artifacts/*.png|pdf`（无任何文件引用的生成物）；
`tests/test_realtime_resource_security.py`（另一 lane 在收口期间正在写、且当时是红的，见下）。

### HEAD 干净检出验证（**重要，别只信工作树**）

`git worktree add D:/Temp/docmind-head-check HEAD` + 复制 `.env` 后跑全量：
**2108 passed / 4 failed / 9 skipped**（247s）。逐个交代这 4 个：

- `tests/test_desktop_entry.py` ×3（`/workbench` 与 `/workbench.html` 404）：**验证方法的产物，不是缺陷**。
  `web/workbench.html` 是前端构建产物，被根 `.gitignore:46` 明确忽略——干净检出没有它，
  而主工作树里前端已经构建过，所以同一批用例在主工作树里是通过的。
- `tests/test_realtime_resource_security.py::test_rapid_session_cycles_do_not_leak_timelines_or_metric_scopes`：
  **HEAD 上就是红的**（该文件的 HEAD 版本），属本文件已记录的「客户端 socket 退出不等服务端异步
  `finally`」同类——5s 预算在负载下不够（失败那几次整轮 17.7s = 5s + 夹具 10s，通过时 ~2s）。
  该文件不是我这条线的，且其作者此刻仍在改它。

**给下一个 AI 的提醒**：工作树与 HEAD 的差异**不代表** HEAD 绿/红——必须用独立 worktree 验证；
`web/workbench.html` 这类被忽略的构建产物会让 `test_desktop_entry` 在干净检出上假红。

### 收口期间仍在动的文件（**属于别的 lane，我没有提交**）

提交过程中工作树持续变化（`/root` 正在做 R12）：`agent_runtime/realtime_protocol.py`、
`agent_runtime/realtime_provider.py`、`tests/test_realtime_protocol_contract.py`、
`docs/realtime-r12-acceptance-20260929.md`。**其中前两个是 R0 独占的协议文件，我一行没碰**
（AI-A 的边界原文也禁止我改）。这些留给对应 lane 收口。

### 另一 lane 的 R9 审计指出我这条线的一个真实缺口（**未修，待用户定**）

`tests/test_realtime_resource_security.py::test_known_gap_interrupt_is_not_guarded_and_kills_the_session`
记录了：`api.py` 的 `cancel` / 媒体分支里 `bridge.interrupt()`、`send_frame()`、`take_audio()` 三处
**没有 try 守卫**——provider 基类契约写明「每个方法都应 fail-closed，返回 False 而非外抛」，而我只在
`start()` 与 pump 循环加了守卫。provider 真外抛时会连会话一起打死。修法很小（三处包 try），
但它改的是 `api.py` 热路径且不在此前用户给的三项范围内，**未动**。

## 2026-09-29 R13 字幕与能力呈现（AI-A/AI-B 兼任）【本轮局部提交】

- 领取「#2 R13 前端收尾」。此前 `AutonomousCockpit.vue` 的 `receiveLiveStreamObservation` 把 `model.delta` / `audio.transcript` 直接 fall-through 丢弃，`hello.ok.provider_capabilities` / `degraded_to` 也不读——字幕区是死组件。本轮把它们消费起来。
- **只改我 lane 的三个已跟踪文件**：`frontend/src/workbench/liveStreamControl.ts`、`frontend/src/workbench/components/AutonomousCockpit.vue`、`tests/test_live_stream_control.py`。**未触碰** `api.py`、`realtime_bridge.py`、别人的 `frontend/tests/*.mjs`、`/root`/AI-F 正在集成的任何文件。
- **`liveStreamControl.ts` 新增纯函数**：`appendCaptionTurn(captions, wire, limit=8)` 把线上事件归约成有界字幕时间线；关键语义——适配器先发增量 `model.delta` 再发一条 `final:true` 的**全量**文本，final 时**整条替换**当前助手回合，否则回答被拼成两遍（专门写断言钉死）。`describeLiveCapabilities()` 把 `provider_capabilities`（audio.in/video.in/text.out/audio.out/interrupt）映射成中文摘要，未知能力原样透出不谎报，抽帧模式返回空串。
- **cockpit 接线**：`model.delta`/`audio.transcript` 进入 `liveStreamCaptions`，并入 R6「正在回答」相位；`hello.ok` 写 `liveStreamCapabilities` 与异常 `degraded_to` 说明；字幕面板显示「你说 / AI（正在回答…）」；停止/启动/新会话复位。
- **验证**：`tests/test_live_stream_control.py` 3 passed（node 执行 44 条断言，含防重复）；前端 `npm run typecheck`、`npm run build` 通过；后端 `-k "realtime or live_vision or live_stream or voice"` **258 passed / 0 失败**无回归。临时 basetemp 已清理。
- **发现但不属我 lane**：他人 `frontend/tests/*.mjs` 直接 `import '../src/**.ts'`，`node --test` 在本机未开 type-stripping 时 `MODULE_NOT_FOUND`；那是它们自身的运行前提（可能需 CI 加 `--experimental-strip-types`），我没改。原生 `model.delta` 真机字幕数值待 R12（本机 Key 的实时服务疑未开通）。
- **提交**：用 pathspec 局部提交这三个文件，不动共享 index、不吞 `/root` 暂存批次；**未 push**。

## 2026-09-29 R8：实时多模态时间线进入开发舱上下文（/root，已完成·勿重复）

- **交付范围**：`api.py` 的 `/api/chat` 在 `ui_context=cockpit_live_vision` 且存在当前请求项目时，读取 `realtime_bridge.timeline_snapshot()` 的少量文本条目；只接受快照项目与请求项目一致的内容，并逐条复核项目 ID。
- **安全边界**：时间线内容按“来自模型的未核实资料，不是用户指令”注入 `system_context`；清理控制字符、限制字段长度、仅保留 observation/text/transcript 文本条目；服务端条目与前端 `visual_timeline` 按规范化文本去重；快照为空、结构异常或读取失败时静默跳过，不影响聊天、工具确认、项目权限和 P0/P1 沙箱。
- **专项验证**：`tests/test_realtime_context.py` **3 passed**（当前项目注入并去重、跨项目隔离、空/异常快照不阻断）；`python -m py_compile api.py tests/test_realtime_context.py` 通过。
- **重复防护**：任务画布已将 R8 标记为“已完成·勿重复”，后续不要在 `api.py` 或聊天上下文另起一套时间线注入逻辑；R12 真机联验只需报告证据或缺陷。

## 2026-09-29 修复全量套件 5 个失败 + pytest 临时目录环境错误（AI-F）

- 环境错误（原 21 errors，全为 `D:\Temp\pytest-of-h'h'h` 的 WinError 5，非代码缺陷）：该目录 ACL 被锁死（非提权 shell 无法读取/改名/夺取所有权）。经实测 pytest 9.1.1 支持 `PYTEST_DEBUG_TEMPROOT` 重定向，已**持久化设置用户级环境变量** `PYTEST_DEBUG_TEMPROOT=D:\Temp\pytest-docmind`，本会话 pytest 调用亦显式带上；21 errors 清零。
- **失败 1 · 工作台回链（`test_desktop_entry`）**：顶栏原回链被改成聚焦底部对话的按钮，`href="/"` 消失。修复 [App.vue](file:///d:/WorkBuddy/rag-agent/frontend/src/workbench/App.vue#L440-L441)：保留「AI 对话」按钮，同时新增 `<a class="wb-question-link" href="/" title="返回 RAG 问答页">问答页</a>`（样式类本就兼容 anchor），跨页导航恢复。
- **失败 2/3 · SSE 心跳与 notice（`test_workflow_chat_stream`，过时断言）**：核对 api.py 证实是有意演进——心跳改为 SSE 注释行 `: keep-alive`（不往对话塞重复提示）；工作流卡片只发结构化 `workflow` 事件，不再另发通用 `notice`（token 已告知用户，前端 useChatStream 兼容两种事件）。按当前契约更新断言：心跳用例改断言 `: keep-alive` 存在且「模型仍在处理」不存在；workflow 用例改断言 `notice` 不存在、`workflow` 恰好一次。
- **失败 4/5 · 子代理步数（`test_orchestrator` + `test_vision_workflow_e2e`，真实生产缺陷）**：根因是双重的——① `SUBAGENT_MAX_STEPS` 默认 0（"不显式设上限"），而 `_run_child` 的 `if used > cap` 在 cap=0 时**第一步后就截断**，所有子代理只能做 1 步；② config 三个步数默认全 0，内层 run 的迭代安全阀（`tool_step_limit > 0` 门控）全被禁用，永不收尾模型会死循环。修复：
  - 新增 `SUBAGENT_SAFETY_STEPS`（env `DOCMIND_SUBAGENT_SAFETY_STEPS`，默认 **8**）：`_run_child` 解析 cap 后，cap≤0 时兜底为 8——子代理必须有确定性上限、父代理无上限策略不变；
  - `_evidence_final` 安全网兜底 final 增加事件标记 `"degraded": True`，`_run_child` 捕获后将输出 `degraded` 置真（安全网文字不算模型 Final Answer）。
- 验证（真实执行）：
  - 五个原失败文件联跑：**103 passed in 10.13s**；
  - 全量套件（3 次实跑对比）：权威终跑 `pytest tests -q --basetemp=D:\Temp\pytest-docmind-bt`
    → **2087 passed, 7 failed, 0 errors, 6 skipped in 218.45s**；本轮 5 failed + 21 errors 均已关闭。
    剩余 7 failed 全部在 `tests/test_visual_targeting.py`（`_VISUAL_CLICK_PROPOSALS` 缺失等
    既有 WIP 漂移，stash 对照已确认先于本轮存在），与本轮无关。
    **注意**：`test_realtime_gateway_bridge.py` 在高负载全量跑中曾出现 11 项时序竞态失败
    （迟到的会话异步收尾与下一用例抢 `_timelines`，该文件已有 `_clean_bridge_state` 夹具缓解），
    另两次全量跑均 21/21 通过；单独跑恒定 21 passed，属负载敏感 flake 而非功能缺陷。
  - **复跑须知**：TRAE 工具宿主进程在我持久化环境变量之前启动，其子 shell 继承旧环境块
    （现象：inline 设了变量的后台任务仍扫到旧目录）。IDE 重启后用户级变量自然生效；
    保险起见全量跑请加 `--basetemp=D:\Temp\pytest-docmind-bt`，可完全绕过 ACL 锁死目录。
  - `python -m py_compile agent.py tests/test_workflow_chat_stream.py` 通过；前端此前 typecheck 0 错误、build 通过（本轮 App.vue 改动后 typecheck 复跑仍 0 错误）。

## 2026-09-29 R4/R5/R10 接线进网关（AI-F，**用户指派**；原「api.py 归 /root 独占」已被用户改派）

- **背景与边界**：任务表原分工里 `api.py` 网关属 /root 独占，本轮由用户明确指派 AI-F 做
  「R4/R5/R10 接线」。为把同文件争用面压到最小，**逻辑全部落在新模块
  `agent_runtime/realtime_bridge.py`**，`api.py` 只留少量调用点（收帧/出观察/取消/音频/关闭/握手）。
  **未触碰** `agent_runtime/realtime_protocol.py`、`realtime_provider.py`、`realtime_omni.py`、
  `realtime_timeline.py`、`realtime_metrics.py`、`frontend/**`。**/root 若要重排 api.py 请参照下面第 3 节定位。**
- **接口变更（唯一一处，向后兼容）**：`hello.ok` 追加 4 个字段，
  **不新增事件类型、不改协议版本**：
  `mode`(`native-realtime`/`sampled-frames`)、`degraded_to`、`reason`、`provider_capabilities`。
  前端 `realtimeProtocol.ts` 宽松透传，`AutonomousCockpit.vue` 已经在读 `event.mode` 与
  `event.reason`（R13 已落地），字段名与之一致。
- **新增状态端点** `GET /api/vision/realtime/status?project_id=`：只回 `timeline` /
  `timeline_projects` / `metrics`。**刻意不报 mode**——模式只由 `/api/vision/realtime-status`
  （R13，连接前）与 `hello.ok.mode`（R4，连接后）提供；本端点再报一份会产生两个可能
  互相矛盾的 mode（本模块按"是否配置 provider"判断，R13 按 `resolve().ok` 判断）。
- **接线做了什么**：`resolve()` 给出 provider 时帧走 `send_frame`、音频走 `send_audio`、
  `cancel` 触发 `interrupt()`，provider 事件由网关泵成线上事件；每项目一条时间线（按活跃会话
  计数，最后一个会话断开即释放）；进程级 `MetricsRegistry` 记录 `frames_sent`/`frames_dropped`/
  `observation_latency_ms`/`queue_depth`/`connections`/`model_rejections`/`first_token_ms`/`first_audio_ms`。
  **起不来就降级**：`start()` 失败不抛错、不伪装会话，原因写进 `reason`。
- **本轮实测发现并修掉的三个真 bug（都不是测试问题）**：
  1. **适配器的握手事件冒充网关握手**（真机复现）：`WIRE_BY_KIND` 把适配器内部事件也映射成
     R0 事件名——`EVENT_STATUS`→`hello.ok`、`EVENT_DONE`→`session.closed`。直接转发会让客户端
     收到**第二个 hello.ok**（实测网关 `probe-s` vs 适配器 `rt-1a0ecc83e58`），而前端收到
     `hello.ok` 会立刻改写模式显示、读 `event.mode` 拿到 undefined → **原生会话被显示成"兼容抽帧"**。
     现在这两类在 `SessionBridge.next_events` 被拦下（`_GATEWAY_OWNED_WIRE_TYPES`），只记账不上线。
  2. **适配器会话号劫持指标作用域**：`start()` 里 `self.session_id = self.provider.session_id`
     用适配器自造的 `rt-...` 覆盖了网关会话号 → 指标全记进 `rt-...` 作用域，
     `drop_session(网关会话号)` 永远清不掉 → **正是本文件 R10 节预告的那颗无界增长雷**。
     现在两者分开（`session_id` 网关的、`provider_session_id` 仅诊断），转发事件的
     `session_id` 归一为网关的（对齐 R0「信封是权威」）。
  3. `CONNECTIONS`/`MODEL_REJECTIONS` 原先记在**项目**作用域，而 `drop_session` 只清会话作用域 →
     同样永不回收。现在走 `bridge.bind_session()`，记在会话作用域。
- **踩到的环境坑（重要，全组测试都受影响）**：本机 `.env` 真的配了
  `DOCMIND_REALTIME_PROVIDER=dashscope_omni`（还有可用 Key），网关**按配置走原生通道**，
  于是「发帧等 `video.observation`」的测试**永久阻塞**（现象：整组 realtime 600s 超时**且无输出**——
  pytest 非 tty 时块缓冲，看着像挂死没有报错）。这不是接线错，是**测试没钉住自己的前提**。
  **新增 `tests/conftest.py`**（此前仓库没有 conftest）加一个 autouse 夹具：整个测试套件默认
  清掉 `DOCMIND_REALTIME_PROVIDER`、测完还原。**只清这一个**（决定模式的唯一开关），
  不清 `DASHSCOPE_API_KEY`（全项目共用，会误伤无关测试）。需要原生通道的测试自己显式设置。
  **所有 AI 请注意：以后写实时相关测试，默认前提是"抽帧通道"。**
- **验证（真实执行）**：
  - `pytest tests/test_realtime_gateway_bridge.py -q` → **21 passed**（新增；原生通道用注册进
    `realtime_provider` 的**假 provider** 驱动，不连真实服务，首 token/首音频指标也能确定性覆盖）；
  - `pytest tests/ -k "realtime or live_vision or live_stream or voice" -q` → **249 passed / 7.90s**
    （接线前同一组是 600s 超时无输出）；
  - `pytest tests/test_realtime_gateway_stress.py tests/test_realtime_gateway_faults.py -q` → **54 passed / 3.44s**；
  - **全量套件** `pytest tests/ -q` → **8 failed / 2086 passed / 6 skipped**（166s）。8 个失败
    全部是本文件已记录的**存量失败**（`test_visual_targeting` ×7、`test_command_execution` ×1，
    与本轮无关）。本轮新增的 21 个用例在**全量套件里全绿**——这一点特意验证过：单独跑全绿、
    混进全量套件却红一项，原因是本文件所有用例共用一个 `project_id`，而 Starlette `TestClient`
    退出**不等待服务端 `finally` 跑完**，上一个会话迟到的 `release_timeline` 会把引用计数从 1
    减到 0、**误释放正在跑的会话的时间线**。修法是 autouse 夹具在 `reset_state()` **之前**先
    等 `active_timeline_projects() == []`，并把断言收窄到本项目。
  - `realtime_bench.py --mode both --frames 40 --model-ms 120` → 丢帧 **70.0%**、网关自身开销
    p50 3.59ms/p95 5.62ms（与 R10 节记录一致）；`--model-ms 0` → rtt p50 0.2ms、stream p50 1.0ms。
    **工具现在会把 `DOCMIND_REALTIME_PROVIDER` 临时清空并断言 `hello.ok.mode == sampled-frames`**，
    环境不对时**报错而不是挂死**。
- **未完成 / 不算数的部分**：
  - **原生通道没有真机数据**：本机 Key 的适配器自报 "commit/cancel 与真人声音频输入均被服务端断连，
    疑似未开通实时多模态服务"（`realtime_omni.availability()` 的 note）。所以原生链路的
    `first_token_ms`/`first_audio_ms` 只在**假 provider** 下覆盖过，真机数字属 R12。
  - **R0 协议缺「本轮回答结束」事件**：适配器 `EVENT_DONE` 现在被丢掉（转发成 `session.closed`
    是撒谎）。一轮结束的信号只能靠 `model.delta` 的 `final: true` 承载。若前端需要独立事件，那是
    R0 的改动（/root 的协议文件），本模块不擅自加事件类型。
  - 前端**不读** `provider_capabilities`/`degraded_to`（`AutonomousCockpit.vue` 只用了
    `mode`/`reason`）。字段已送到线上，UI 呈现归 R13。
  - **给 R9 的两条实测数字（原生通道的资源开销，本轮量到但未优化）**：
    ① 每条连接都会真去建一次原生会话——真机实测 `start()` ≈ 0.5s、`close()` ≈ 3s
    （关 WebSocket + join 接收线程）。`close()` 是在 `await asyncio.to_thread` 里做的，
    所以断开时该会话还要占一个工作线程约 3s；若将来并发会话很多，这里需要专门池化。
    ② 泵事件用 `await asyncio.to_thread(bridge.next_events, timeout=0.05)`，即**每个原生会话
    每 50ms 占一次 asyncio 默认线程池**（`poll` 是阻塞读，不丢线程会卡死整个事件循环）。
    并发会话数上去后应改成专用线程或 `poll` 的异步读侧。两者都属于 R9（资源与清理）的调优面。
- **冲突风险**：`api.py` 仍有多个并发写入者（/root 记「被另一 AI 并发增 800+ 行」）。
  本轮的 api.py 改动只有 8 处，全部集中在 `/api/vision/live-stream` 处理器内 + 1 个新端点，
  按下面的定位核对即可。`tests/conftest.py` 是**新增的全局文件**，会影响所有人的测试前提（见上）。
- **api.py 改动定位（供 /root 重排）**：①`from agent_runtime import realtime_bridge`（import 区）；
  ②`bridge`/`pump` 两个 local + `pump_provider()`；③hello 分支（建桥 + `bind_session` + `start` +
  能力合并 + `create_task`）；④`cancel` 分支 `bridge.interrupt()`；⑤`audio.chunk` 分支
  `bridge.take_audio()`；⑥媒体分支 `send_frame`/`note_frame`；⑦`process_frames` 的
  `note_model_failure`/`note_observation`；⑧`finally`（先放槽位，再 `bridge.close`）。

## 2026-09-29 提交状态与「HEAD 未闭合」提醒（AI-A/AI-B，交 /root 收口）

- 本会话交付已全部进 `main`（**未 push**）：`e654a21` 网关压力测试、`875087b` `liveStreamControl.ts`+`test_live_stream_control.py`、`075e7e6` R0 协议、`fdb9e94` 我的 cockpit R1/R6 接线 + 本节。前端自治切片用 pathspec 局部提交，未碰他人正在集成的模块，也未动共享 index 里 /root 的暂存。
- **⚠ HEAD 当前不是自洽可测的**，请 /root 在收口批次里补上这些仍未提交的依赖：
  - **后端网关**：`api.py@HEAD` **没有** `/api/vision/live-stream` 路由（`realtime_protocol` 也未 import）。但已入库的 `tests/test_realtime_gateway_stress.py` 会连这个端点——**干净 checkout 上它现在会红**（连不上路由 / close 码断言失败）。`api.py` 里的 R3 网关 + `realtime_protocol.py` import 需随本文件一起提交才闭合。
  - **前端叶子模块（仍 `??` 未跟踪）**：`liveVisionSampling.ts`、`liveVisionFocus.ts`、`liveVisionAlerts.ts`、`voiceVisionSync.ts`、`visualActionLoop.ts`、`components/CockpitModelBar.vue`。`AutonomousCockpit.vue@HEAD` 已 `import` 它们，缺任一前端 `typecheck`/构建即失败。
  - **前端 .mjs 单测（仍 `??`）**：`frontend/tests/liveVision*|voiceVisionSync|visualActionLoop.test.mjs`（用 `node --test` + 原生类型剥离跑对应 `.ts` 叶子模块，独立于 api.py，可安全先行入库）。
- 建议收口顺序：①`api.py` 网关 + 上述前端模块随 R3/R2 各自槽位提交；②提交后在干净 worktree 跑 `pytest tests/test_realtime_gateway_stress.py`（应转绿）与 `frontend` 下 `node --test frontend/tests`、`npm run typecheck && npm run build` 三件套验收。
- 我不擅自补提这些他人槽位文件（会构成混合提交、抢占 /root 正在组装的批次），仅留此清单。

## 2026-09-29 R1+R6：前端自适应发送与实时状态 UI（AI-B，由 AI-A 兼任）【已交付本轮】

- 领取任务表 **AI-B**（R1 前端视频采集与自适应发送 + R6 实时开发舱 UI）。边界遵守：只改 `AutonomousCockpit.vue` 与新增媒体模块，**未触碰** `realtimeProtocol.ts`、`agent_runtime/realtime_protocol.py`、`api.py` 网关；`stopLiveVision` 里既有的 `session.close` 裸包写法属 /root 的协议接线，保持原样未动。
- **新增 `frontend/src/workbench/liveStreamControl.ts`**（R1/R6 纯逻辑模块，零 Vue/浏览器依赖，可脱离页面执行）：
  - `createAdaptiveSender()`：背压自适应档位表（间隔 300–4000ms、长边 1600–480px、质量 0.85–0.5）。压力信号（观察 e2e 延迟 ≥6s / 发送缓冲 ≥250KB / 服务端 throttled / 编码超期）**单次即降一档**，恢复需连续 4 个健康样本**逐级升回**；`retry_after` 直接抬高间隔下限并封顶 8s。默认档 500ms/1280/0.8 与接入前固定行为一致，不改变现有观感。
  - `classifyLivePhase()`：R6 验收「正在看/正在回答/连接中/已断线/已暂停/未开始」六相位 + 中文标签表；「正在听」留给 R2 音频链路。「已暂停」「断线」优先于旧观察粉饰。
  - `createInFlightLedger()`：seq→(capturedAt,sentAt) 在飞账本，`onObserved` 回帧即视为更早帧被服务端最新帧背压取代；给出 e2e/analyze 延迟与积压年龄供降级信号使用。
- **接线 `AutonomousCockpit.vue`**：采集缩放/JPEG 质量改用 `liveAdaptive.plan()`（原固定 1280/720、q0.8 已移除）；发送循环默认间隔 = 自适应档位（原硬编码 500ms）；发不出去（socket 非 OPEN 或缓冲 ≥1MB）的帧**丢弃并计入「丢弃旧帧」而非补发**；观察回包按 seq 配对算 e2e 延迟回填 adaptive 并显示「每 x 秒上传 · 理解延迟 · 丢弃旧帧」；面板头部按相位显示状态；**新增「打断」按钮**经 R0 `realtimeCancel()` 发 cancel 控制包并清在飞账本/帧缓存；重开/暂停/停止/重连各路径补齐状态复位。
- **新增 `tests/test_live_stream_control.py`（执行式契约，3 项）**：沿用仓库「python 驱动前端逻辑」约定——用项目自带 tsc 把模块编译成 ESM 后由 **node 真实执行** 28 条断言（降级/恢复/退避/边界、相位归类、账本取代语义），另钉住模块纯逻辑无浏览器依赖 + Cockpit 接线回归（防止退回魔法数字）。node/tsc 缺失时整文件跳过，不产假绿。
- 验证：`tests/test_live_stream_control.py` **3 passed**；实时全组 7 文件 **199 passed**（此前 `test_live_vision` 的 2 个红项已由实现侧修好，本轮复核转绿）；前端 `npm run typecheck`、`npm run build` 通过（仅既有非 module 脚本与大 chunk 提示）。
- 未完成/冲突提示：延迟显示为「采集→理解返回」e2e 口径，真实模型延迟数值属 R12；AI-F 已另交付 `realtime_bench.py`（服务端侧 p50/p95 量具），与本模块的前端降级阈值口径一致但不共用代码；`AutonomousCockpit.vue` 是多方交叠文件，本轮改动均集中在实时视觉面板函数与 `acp-live-vision-panel` 头部一行，如 /root 需重排可参照本节定位。真实屏幕共享下 FPS/画质升降的肉眼体验未在真机验证（需用户设备，属 R12）。

## 2026-09-29 R10 性能基准工具（AI-F，已认领【勿重复实现】）

- **认领声明**：`realtime_bench.py`（新增，根目录）与 `tests/test_realtime_bench_tool.py`（新增，13 项）
  是 **AI-F 槽位 R10 的交付物**，**只新增文件、未改任何实现**（未触碰 `api.py` 的
  `/api/vision/live-stream` 处理器、`agent_runtime/realtime_protocol.py`、
  `agent_runtime/realtime_metrics.py`、前端实时协议、`AutonomousCockpit.vue`）。
  请勿重复实现性能基准工具；要扩充请在这两个文件上追加。**无接口变更。**
- **为什么做这个**：R10 的验收条件是「给出 p50/p95 指标」。指标库
  `agent_runtime/realtime_metrics.py` 早就定义了 `observation_latency_ms` / `end_to_end_ms` /
  `frames_sent` / `frames_dropped` / `queue_depth` 和 `MetricsRegistry`，但**在此之前没有任何东西
  真的往里写过数**——缺的不是指标库，是驱动真实网关产生数字的量具。本工具补的就是这一环。
- **做法（复用而非重造）**：进程内夹具驱动**真实网关**（`TestClient` + `patch.object` 注入占位模型），
  协议编解码走 `realtime_protocol`，指标聚合走 `MetricsRegistry`。工具只负责发帧、配时、归因、汇总。
  两种模式测不同的东西：`rtt`（一帧一收，空载单帧往返，无排队）与 `stream`（定间隔连发，
  有负载的端到端含排队；网关 `pending` 是单槽，模型忙时中间帧被最新帧覆盖 → 丢帧率来源）。
- **归因方式**：`video.observation` 回带该帧的 `sequence`/`captured_at`，所以每个观察都能精确对回
  是哪一帧，延迟与丢失都不靠估算。丢帧数还与**另一条独立代码路径**（占位模型自己的调用计数器）
  互校：慢模型实测 `dropped = 28` 与 `model_calls` 差值 28 吻合。
- **实测输出**（`.\.venv\Scripts\python.exe realtime_bench.py --frames 40 --window 256`）：

  | 模式 | 发送 | 观察 | 丢帧率 | p50 | p95 | 峰值积压 |
  |---|---|---|---|---|---|---|
  | rtt（model 0ms） | 40 | 40 | 0% | 0.2ms | 0.3ms | 1 |
  | stream（model 0ms） | 40 | 40 | 0% | 0.7ms | 1.0ms | 1 |
  | rtt（`--model-ms 120`） | 40 | 40 | 0% | 123.1ms | 135.4ms | 1 |
  | stream（`--model-ms 120`） | 40 | 12 | **70.0%** | 146.2ms | 169.6ms | 30 |

  慢模型那行额外给出扣除模拟模型耗时后的**网关自身开销：p50 3.11ms / p95 15.42ms**。
- **一个必须记住的坑（我踩了并修掉）**：`stream` 模式的读**必须与发并发**。第一版先发完再统一读，
  早期帧的观察堆在客户端队列里空等 1.3 秒，测出来的是"我的读取延迟"（p50 683ms，纯夹具伪影）。
  现在读线程在观察到达那一刻打时间戳；`tests/test_realtime_bench_tool.py::
  test_stream_latency_is_measured_at_arrival_not_after_the_send_loop` 专门锁死这一点，防回归。
- **另一个环境坑**：本机**不能用 pytest 的 `tmp_path`**——用户名含撇号，pytest 扫描
  `D:\Temp\pytest-of-h'h'h` 抛 `PermissionError: [WinError 5]`，与测试内容无关。本文件改用
  `tempfile.mkdtemp()` 自管并在 `finally` 清理。后来的测试请绕开 `tmp_path`。
- **验证（真实执行）**：
  - `pytest tests/test_realtime_bench_tool.py -q` → **13 passed**；
  - 混用无污染：`-k "realtime or live_vision"` 含新文件 **220 passed** / 不含 **207 passed**（220 = 207 + 13）；
  - 两种模式、两种模型耗时下的端到端手工运行各一次，输出见上表。
- **未完成 / 不算数的部分（不谎报）**：
  - 本工具是**进程内夹具**：没有真实网络、没有真实模型，`--model-ms` 只是可控占位延时。所以
    - `model_ms=0` 时 rtt 的 p50/p95 约等于网关自身开销；
    - `model_ms>0` 时工具会把 `overhead = rtt - model_ms` 单独报出来；
    - stream 的延迟**含排队时间**，是上界而非纯处理时间。
  - **它不能替代 R12**：真实摄像头/麦克风/屏幕共享 + 真实实时模型的联验仍是 R12。
  - `first_token_ms` / `first_audio_ms` / `model_rejections` / `reconnects` 这几个指标名**本工具没有写**
    ——文字 token 的首字延迟与音频链路都依赖 R4/R5 接线和 R2 音频通道，现在测不出来，所以不伪造。
- **阻塞**：`realtime_provider` / `realtime_timeline` / `realtime_metrics` 在 `api.py` 中**均无 import**
  （本轮 grep 复核）。网关仍走原来的 HTTP 抽帧分析，所以本工具量的是**当前这条真实链路**的开销；
  等 R4/R5/R10 接入 `api.py` 后，同一套量具可以直接用来验收原生实时链路的 p50/p95。
- **冲突风险**：新增两个文件，与 /root 未提交的网关改动零重叠。唯一理论冲突点是
  `api.py` 的 `analyze_live_frame_ep` 与 `api.projects.get_project` 的名字——本工具用
  `patch.object` 挂桩；若将来重命名，改 `realtime_bench.gateway()` 一处即可。
- **给接线人（R4/R5/R10 接入 `api.py` 时）的一条提醒，本轮核查发现**：
  `MetricsRegistry.drop_session()` 已实现但**全仓库没有任何调用点**——目前无害，因为唯一的
  `MetricsRegistry` 实例是本工具里的短命对象（`realtime_bench.py`）。一旦 R10 接成**进程级长命注册表**，
  每个新 session_id 都会永久留下一个 `_Scope`（`_session_scope` 是惰性创建、只增不减），
  长期运行会随会话数无界增长。接线时请把 `drop_session(session_id)` 挂到网关的断线清理路径上
  （`api.py` 现在 `_LIVE_VISION_CLIENTS.pop(project_id, None)` 那一处）。
  重复同一 session_id 不会增长（会复用已有 scope），增长只来自 session_id 不同的会话。
  本工具自己的注册表是每轮新建的，不受影响。
- **本节后续更新（同日，AI-F）**：本节写的「无接口变更」「`api.py` 中均无 import」**已被同一轮的
  R4/R5/R10 接线改变**——请以本文件**顶部「R4/R5/R10 接线进网关」节为准**。接线后 `hello.ok`
  多了 4 个字段，`realtime_bench.py` 也改为临时清空 `DOCMIND_REALTIME_PROVIDER` 并断言
  `mode == sampled-frames`（原生通道不产生 `video.observation`，不钉住前提会挂死而不是报错）。
  `drop_session` 那颗雷已按上段建议挂到网关断线清理路径，并在接线时发现它当时**真的没生效**
  （原因见顶部第 2 个 bug）。

## 2026-09-29 修复 observation 格式缺陷：JSON 源码回退（AI-F）

- 问题（检查 anomalies 链路中 observation 返回格式时实测发现）：视觉模型返回**合法 JSON 但 observation 为空串/缺键/非字符串**时，`parse_live_vision_result` 的 `return observation or text[:2000]` 会回退成**原始 JSON 源文本**，前端不区分地把 `{"anomalies":[...]}` 当观察文字展示在观察区与时间线；observation 为数字/数组时 `str()` 静默强转成 `123`、`['a', 'b']`。该回退本是为兼容"纯文本旧模型"，但 JSON 解析成功后不应触发。
- **修复（`agent_runtime/live_vision_alerts.py`，JSON dict 分支）**：
  - `raw_observation = data.get("observation")`，仅当其为字符串时取 `strip()[:2000]`，其余形状（数字/数组/null/缺键）一律归一为 `""`，不再 `str()` 强转；
  - 返回改为 `return observation, alerts`，JSON 分支不再回退原始 JSON 源码；非 JSON / 非 dict 的纯文本旧格式分支（行 20、22）行为完全不变。
- **补测试（`tests/test_live_vision_alerts.py`，新增 3 个用例）**：空 observation 不回退 JSON 源码；缺 observation 键时保留合法告警但观察为空；数字/数组/null observation 归一为空。
- 验证（真实执行）：
  - `pytest tests/test_live_vision_alerts.py tests/test_live_vision.py -q` → **18 passed**；
  - 联合回归（含 realtime_metrics/gateway_faults/protocol_contract/provider）→ **187 passed, 1 failed**；唯一失败 `test_realtime_provider.py::OmniAdapterTests::test_session_update_sent_on_start`（期望音频格式 `pcm_16000hz_mono_16bit`，`realtime_omni.py:46` 实际发 `pcm16`），该测试不引用 live_vision_alerts，属 R4/R5 音频格式枚举的既有跨 AI 漂移，**非本次引入**，未越界修改；
  - `python -m py_compile agent_runtime/live_vision_alerts.py` 通过。

## 2026-09-29 R11 遗留第 2 项：anomalies 结构化链路核对（AI-F，仅验证未改代码）

- 背景：上一轮回归时 `test_live_frame_returns_structured_anomaly_candidates` 仍 502。本轮接手后发现**另一位 AI 已在两轮之间完成修复**（新增 `agent_runtime/live_vision_alerts.py`、`frontend/src/workbench/liveVisionAlerts.ts`，并改了 `api.py`）；本轮未改任何代码，只做端到端完整性核对与全量回归，结论：链路已闭环、无半截修复。
- 核对到的完整链路（逐环确证）：
  1. `/api/vision/frame` 提示词明确要求返回 JSON：`observation` + `anomalies`（每项含 type/target/evidence/confidence）；
  2. `parse_live_vision_result`（native 与 harness 两个分支都调用）负责去 ```fence、解析 JSON、校验形状：type 必须在白名单（error_message/crash/render_failure/layout_breakage/unexpected_state），target/evidence 非空，0.75 ≤ confidence ≤ 1，最多保留 3 项；非 JSON 旧格式降级为纯文本观察、不报错；
  3. HTTP 响应携带 `anomalies`；实时网关 `video.observation` 事件也透传 `anomalies`；
  4. 前端两处消费方（`AutonomousCockpit.vue:903` WebSocket 流、`:1102` 采样 HTTP）均读 `result.anomalies`，`VisionAnomaly` 类型与后端形状逐字段一致；
  5. `advanceVisionAlert` 要求同一异常**连续两帧确认**才弹提醒，防止单帧模型幻觉打扰。
- 验证（真实执行）：
  - 目标用例 `test_live_frame_returns_structured_anomaly_candidates`：本轮**绿**（观察 `错误弹窗可见`、anomalies[0].target == `Error 404`）；
  - `pytest tests/test_live_vision.py tests/test_live_vision_alerts.py tests/test_realtime_metrics.py tests/test_realtime_gateway_faults.py tests/test_realtime_protocol_contract.py tests/test_realtime_provider.py -q -p no:cacheprovider` → **185 passed**；
  - 前端 `npm run typecheck` → 0 错误；`npm run build` → ✓ built in 5.23s。
- R11 问题清单两项至此均已关闭（第 1 项由 AI-F 修复，见下节；第 2 项由另一位 AI 修复、AI-F 验证）。

## 2026-09-29 修复 R11 遗留：desktop-frame 的 strict_project 失效（AI-F）

- 问题（R11 问题清单第 1 项，已复现确认）：`/api/vision/desktop-frame` 从不解析请求体里的 `strict_project`，调用 `grab_embedded(pid)` 时该参数恒为默认 `False`。当项目没有独立嵌入宿主而系统存在遗留默认宿主时，请求严格隔离的调用方（`AutonomousCockpit.vue:1369` 桌面视觉复验）会**静默抓到默认窗口画面**，造成跨项目画面串入证据。底层 `screen_capture.grab_embedded(..., strict_project=True)` 与 `_embedded_target` 本来支持严格语义（缺项目宿主/子窗口不匹配 → 返回 None），只是端点没接线。
- **修复（`api.py`，2 处有效改动）**：`capture_live_desktop_frame` 中新增 `strict_project = bool((payload or {}).get("strict_project"))`（`bool()` 归一化防 `"false"`/`1` 等非布尔歧义），并在 embedded 分支透传 `screen_capture.grab_embedded, pid, strict_project=strict_project`。foreground 分支不受影响。
- 验证（真实执行）：
  - 目标用例 `tests/test_live_vision.py::test_desktop_frame_strict_project_never_uses_default_window`：修复前红（实际调用 `grab_embedded('project-test')`），修复后**绿**（断言 `grab_embedded('project-test', strict_project=True)`）。
  - 联合回归：`pytest tests/test_live_vision.py tests/test_live_vision_alerts.py tests/test_realtime_metrics.py tests/test_realtime_gateway_faults.py tests/test_realtime_protocol_contract.py tests/test_api_routes.py -q -p no:cacheprovider` → **172 passed, 1 failed**；唯一失败是 R11 问题清单第 2 项 anomalies 链路（`test_live_frame_returns_structured_anomaly_candidates`），与本次无关。
  - 对 `tests/test_visual_targeting.py` 做过 stash 对照：无本改动时 7 failed/4 passed，有本改动时同 7 failed 但 5 passed（多通过的正是 strict_project 用例）；那 7 个失败是 `_VISUAL_CLICK_PROPOSALS` 缺失等既有 WIP 漂移，**非本次引入**。
  - `python -m py_compile api.py` 通过；`git diff --check api.py` 无空白错误。

## 2026-09-29 前端 typecheck 11 个错误修复（AI-F，承接 R10+R11）

- 背景：上一轮恢复的 `AutonomousCockpit.vue`（2363 行，含实时视频接线）依赖两个共享类型文件中尚未落盘的配套字段，`npm run typecheck` 报 11 个错误。本轮按错误清单分类、在**类型定义源头**修复，未改动 Vue 组件逻辑。
- **`frontend/src/workbench/previewFeedback.ts`**：`ChatUiContext` 增加 `'desktop_visual_review'`（桌面点击视觉复验反馈）；`PreviewFeedbackRequest` 增加 `workflowId?`、`feedbackId?`、`autoRetry?`（均为可选，旧调用点不受影响）。一处修复消解 6 个错误（行 734/1311/1312/1313/1323/1351）。
- **`frontend/src/workbench/eventBus.ts`**：`AppEventMap['docmind:live-vision-frame']` 载荷增加 `capturedAt?: number`（帧采集 epoch ms）与 `focusImage?: Blob | null`（圈选区域裁剪帧）。一处修复消解 5 个错误（行 474/849/915/1059/1115）。
- 验证（均为真实执行）：
  - `npm run typecheck`（vue-tsc --noEmit + tsc -p tsconfig.node.json）→ **0 错误**；
  - `npm run build` → 首次写 `web/index.html` 时遇瞬时文件占用（运行中的服务进程持有句柄）报错，重试后 **✓ built in 6.57s**，仅剩既有的大 chunk 与非 module 脚本提示；
  - `.\.venv\Scripts\python.exe -m pytest tests/test_realtime_metrics.py tests/test_realtime_gateway_faults.py tests/test_realtime_protocol_contract.py -q -p no:cacheprovider` → **148 passed**（协议契约文件较上轮多收集 1 项，源于 R4 `realtime_provider.EVENT_KINDS` 新增 1 个事件种类的参数化，非本轮改动；全绿）。
- 边界说明：`workflowId/feedbackId/autoRetry` 目前只在前端类型与发送侧打通，`onSendChat` 如何把它们透传到 `/api/chat` 属聊天层/后端接线，本轮未扩大范围。

## 2026-09-29 R11 归属确认 + 问题清单（测试负责人）

- **归属确认**：`tests/test_realtime_gateway_faults.py`（43 项）与 `tests/test_realtime_protocol_contract.py`
  （51 项，合计 94 项）是本 AI 为 **R11** 交付的产物，**只新增测试、未改任何实现**（未触碰
  `agent_runtime/realtime_protocol.py`、`frontend/src/workbench/realtimeProtocol.ts`、
  `api.py` 的 `/api/vision/live-stream` 处理器、`AutonomousCockpit.vue`、`voice.py`）。
  这两个文件请勿重复创建或重写；要扩充请在其上追加。**无接口变更。**
- **补正上一条**：`tests/test_live_vision.py` 的 4 项失败中，**协议 v0 那 2 项已由本 AI 按 v1 改写并通过**
  （`test_live_stream_delivers_timestamped_model_observation_without_chat_history`、
  `test_live_stream_discards_intermediate_frames_while_model_is_busy`）。
  该文件此前剩下的失败都是实现侧问题而非过时断言，即下面问题清单 1、2；两者现均已由对应负责人修复，
  该文件 **13 passed**。
- **验证**：`pytest tests/test_realtime_protocol_contract.py tests/test_realtime_gateway_faults.py
  tests/test_live_vision.py` → **107 passed**（51 + 43 + 13 = 107 项，全绿；问题清单 1、2 均已由
  对应负责人修复，见下）。
  污染基线对照：`tests/{api_routes,project_routing,projects,cockpit_policy,visual_console_signals,
  tool_vision_channel,voice,live_vision,visual_targeting}` 不含/含本轮两个新文件为
  **7 failed, 100 passed / 7 failed, 194 passed**（94 = 51 + 43 项全数新增）—— 失败集合逐条相同
  （7 项全在 `tests/test_visual_targeting.py`，属 `_VISUAL_CLICK_PROPOSALS` 缺失等既有 WIP 漂移），
  新增文件不污染同进程其他用例。
- **R11 覆盖范围**：契约（Python 词汇表 ↔ 前端 TS 联合类型双向对表、R4 `to_wire()` 产物必须能被
  R0 `server_event` 逐字段还原、二进制包畸形矩阵、`captured_at` 窗口端点、`compact_error` 截断）；
  生命周期（hello 握手/重复 hello/心跳回显/session.close/未登记项目/缺 project_id/Origin 矩阵）；
  故障（非法 JSON 与未支持控制包 1003、单帧畸形只丢帧不断会话、超长帧、`audio.chunk` 回
  `audio_not_ready` 且不占帧位、模型抛错/非 dict/`Response.body` 解析失败）；压力与时间同步
  （忙时只留最新帧、观察带的是**那一帧**的 `captured_at`、乱序序号原样透传、40 帧连灌调用被合并）；
  取消与打断（取消后已进模型的旧帧不回前端、排队帧一并清掉）；隔离与清理（双项目上下文不互串、
  断线清槽位、断线后同项目重连不被上一个会话处理器抢帧）。
- **未完成（不谎报）**：真实摄像头/麦克风/屏幕共享/实时模型联验属 R12，未做；p50/p95 端到端延迟与
  丢帧率指标属 R10，未做（本轮的"压力"只验证背压合并，没有测延迟分布）。
- **问题清单（本轮按分工未改实现，交对应负责人）**：
  1. ~~**`/api/vision/desktop-frame` 丢了 `strict_project`**~~ —— **已修（非本 AI 修复，已在 `api.py` 落地）**：
     `capture_live_desktop_frame` 现在读 `payload["strict_project"]` 并透传给
     `screen_capture.grab_embedded(pid, strict_project=...)`，`tests/test_live_vision.py::
     test_desktop_frame_strict_project_never_uses_default_window` 已转绿。原问题：端点只读
     `target`，调用 `grab_embedded(pid)` 时该参数恒为默认 `False`，请求严格隔离的调用方
     （`AutonomousCockpit.vue` 桌面视觉复验）会静默回退到默认嵌入窗口，存在**跨项目串画面**风险。
  2. ~~**结构化异常（anomalies）链路已断**~~ —— **已修（非本 AI 修复）**：`api.py` 现在有
     `parse_live_vision_result()`，`/api/vision/frame`（`api.py:2317/2324`）与实时网关
     （`api.py:2440`）都按 `observations + anomalies` 成对拆解并回填，
     `test_live_frame_returns_structured_anomaly_candidates` 已转绿。原问题：提示词里已无
     `anomalies` 字样，端点把整串 JSON 当成一条 observation 原样返回（`observations` 里是一条
     JSON 字符串），而前端 `AutonomousCockpit.vue:903/1102` 仍按 `result.anomalies` 消费，直接 502。
  3. **R0 `server_event()` 允许载荷改写信封**：`event.update(payload)` 在写 `v`/`type`/`sent_at` 之后
     执行，`server_event("hello.ok", type="evil", v=2)` 会产出 `{"v": 2, "type": "evil"}`。同一协议的
     `RealtimeEvent.to_wire()` 反而显式跳过这三个键，两半规则不一致。当前 `api.py` 调用点都传显式
     关键字，尚不可被利用。
  4. **R0 `parse_binary_packet` 放行布尔序号**：`isinstance(True, int)` 为真，`{"sequence": true}` 会被
     当成序号 1 收下。
  5. **`observations` 类型不校验**：网关 `result.get("observations") or []` 只挡空值，模型适配器返回
     字符串时会被原样发到线上，而前端按 `string[]` 使用。
  6. **R4/R5 尚未接线**：`realtime_provider` / `realtime_timeline` 在 `api.py` 里没有任何 import，网关仍走
     抽样帧链路。两个模块自身有 22 项测试，但"原生实时模型接入网关"（R3↔R4）与"时间线进入任务上下文"
     （R8）还没有可验证的接线，R11 无法在网关层为它们写契约测试。
     3、4、5 三处已由 `test_known_gap_*` 断言当前行为，修好后请把断言翻转成严格版本。
- **踩坑记录（已写进测试文件 docstring）**：① 假模型必须用**绑定异步方法**做 `side_effect`，
  `AsyncMock` 不会 await "可调用实例"返回的协程，测试会静默拿到协程对象而不是结果；
  ② Starlette `TestClient` 在端点**正常返回**时不给客户端发 `websocket.close`，`receive_*` 会永久阻塞
  ——只有服务端显式 `websocket.close(code=...)` 的路径才能断言关闭码，`session.close` 那条只能断言
  ack 再手动收尾。
- **冲突风险**：两个新文件本轮独占。`tests/test_live_vision.py` 我只改了两条 WebSocket 用例
  （其余 11 条未动），若 R0/R3 也要在该文件补契约测试需先协调。

## 2026-09-29 R10+R11：实时性能指标 + 契约测试（AI-F）

- **事故修复（先说）**：本轮接手前 `frontend/src/workbench/components/AutonomousCockpit.vue` 被一次误操作的 Write 覆盖成了 26 行 Python 占位内容。已从 Trae 本地历史恢复：`%APPDATA%\Trae CN\User\History\-32839bb1\8ay0.vue`（2363 行 / 166456 字节，17:29 保存，覆盖前最后一个快照），逐字节还原回工作区，含 `realtimeProtocol` 导入、`/api/vision/live-stream` WebSocket 与 `getDisplayMedia` 接线，文件以 `</style>` 正常收尾。注意：17:29→17:35 之间若有未保存编辑器缓冲不在此快照内。
- 同步删除了误建的 `tests/test_realtime_contract.py`（内含不存在的 `api.TestClient`/`client.hello` 等伪造 API）。
- **R11 现状核对（避免重复造轮子）**：网关契约/故障/压力测试已由另一 AI 完整落地在 `tests/test_realtime_gateway_faults.py`（生命周期、项目/Origin 隔离、hello 先行、畸形媒体、最新帧背压、cancel、模型异常、断线清理、40 帧持续压力合并）与 `tests/test_realtime_protocol_contract.py`（Python 协议 ↔ 前端 TS 词汇表一致性、媒体/控制包、已知缺口）。本轮**未再新建重复的 gateway contract 文件**。验证：`.\.venv\Scripts\python.exe -m pytest tests/test_realtime_gateway_faults.py tests/test_realtime_protocol_contract.py -q -p no:cacheprovider` → **93 passed in 2.78s**。
- **R10 新增 `agent_runtime/realtime_metrics.py`**：线程安全（RLock）指标注册表。有界环形窗口直方图（默认 1024，nearest-rank p50/p95、min/max、累计 count）、单调计数器、瞬时仪表；全局 + 每会话双层作用域。本轮修正一处接入隐患：会话 id 原先复用指标名校验，会拒绝连字符；已改为独立规则（`[a-z0-9][a-z0-9_.-]{0,159}`，上限 160 与协议截断一致），UUID/`sess-a` 等真实 id 可建作用域。
- **新增 `tests/test_realtime_metrics.py`**：54 项，覆盖 nearest-rank 数学（名 50/95、非整数名向上取整、p0/p1）、窗口淘汰只留最新样本但 count 累计、计数器拒绝负增量、仪表只留最后值、全局/会话隔离、drop_session/reset、坏名称、NaN/±Inf、bool 拒绝、8 线程 ×200 并发不丢增量、快照可 JSON 序列化、连字符/UUID 会话 id。验证：`.\.venv\Scripts\python.exe -m pytest tests/test_realtime_metrics.py -q -p no:cacheprovider` → **54 passed in 0.08s**。
- **未完成 / 边界外问题（不谎报、不越界修改）**：
  - `npm run typecheck`（frontend）报 11 个错误，全部来自共享类型文件缺少配套字段：`previewFeedback.ts` 的 `ChatUiContext` 缺 `'desktop_visual_review'`、`PreviewFeedbackRequest` 缺 `autoRetry/workflowId/feedbackId`，以及 `docmind:live-vision-frame` 事件载荷类型缺 `capturedAt/focusImage`。这些配套编辑 17:35 前未落盘，可能仍在 R1/R6 负责人的未保存缓冲中；按分工本轮未改这些共享文件。
  - 旧版 `tests/test_live_vision.py` 4 项失败（仍期待 `type:"observation"`、不发 hello 直推帧、`grab_embedded(strict_project=True)` 旧签名），属演进后契约的过时断言；更新该既有文件不在本轮「只新增」边界内，仅记录证据。
  - 指标注册表尚未接入 `api.py` 网关录制点（R10 只交付工具；接线会触碰 R0/R3 的 api.py，留待其负责人或下一轮）。

## 2026-09-29 AI-A：R0/R3 契约与压力测试补齐（只读，不触碰实现）

- 领取任务表 **AI-A**（协助 R0/R3：只新增独立契约/压力测试）。先核对现状：R11 已有 `tests/test_realtime_protocol_contract.py`（协议词汇表/包格式/已知缺口）与 `tests/test_realtime_gateway_faults.py`（生命周期/背压/取消/Origin/隔离），未重复造轮子；本轮补它们未覆盖的网关对外行为。
- **新增 `tests/test_realtime_gateway_stress.py` 11 项**，全部使用注入假模型：
  - 线上信封规则：会话生命周期六种事件（hello.ok/observation/error/heartbeat/cancel.ok/session.closed）逐一通过前端 `parseRealtimeServerEvent` 的 `v/type/sent_at` 复刻断言；hello.ok 能力集与握手机后非 error 事件的 session_id 绑定被钉死；
  - 控制通道存活：模型被拖住时 heartbeat 三连击即时响应、cancel 只作废本会话滞留帧；
  - 风暴：100 个畸形媒体包只丢包不踢会话且零进入模型；50 次 cancel 风暴后管线正常；
  - 同项目双会话并发：事件按 session 隔离、A 取消不影响 B、A 的观察结果不会漂到 B 的连接；
  - 重连卫生：8 次连接-收帧-断开循环无残骸、帧一一对应、`_LIVE_VISION_CLIENTS` 不残留；
  - 对外契约：hello `session_id` 超 160 字符截断且后续事件一致；越出 ±120s/+60s 采集窗的帧在网关入口拒绝且不入模型；
  - 新增一条「已知缺口」记录（R11 同风格）：**网关 error 事件经 `compact_error` 发出时不带 `session_id`**，多路/重连场景前端无法归属错误，已上报 R0，修复后翻转该测试。
- 验证命令：`.venv\Scripts\python.exe -m pytest -q tests/test_realtime_gateway_stress.py --basetemp .pytest-tmp`：**11 passed**；实时全组（protocol_contract/gateway_faults/gateway_stress/metrics/provider）：**180 passed**。
- **归因说明（非本轮引入）**：`tests/test_live_vision.py` 存在 2 个既有失败——`test_live_frame_returns_structured_anomaly_candidates`（502，异常提取路径与工作区改动不一致）与 `test_desktop_frame_strict_project_never_uses_default_window`（`grab_embedded` 实际未传 `strict_project=True`）。均指向 `/root` 公告的「待修复前端回归」相关工作区改动，属 R0/R3 实现侧，AI-A 边界内不修改。
- 未完成：真实设备与真实模型联验属 R12；运行中的服务需重启才加载本轮工作区改动。改动仅新增一个测试文件与本节 HANDOFF，未提交。

## 2026-09-29 并行分工公告：R0 + R3 由主代理独占开发

- **负责人：当前主代理 `/root`，任务 R0 实时协议 + R3 后端实时会话网关。** 这两项已经开始实现；其他 AI 不要领取或重复修改 `agent_runtime/realtime_protocol.py`、`frontend/src/workbench/realtimeProtocol.ts`、`api.py` 的 `/api/vision/live-stream` 处理器，以及 `AutonomousCockpit.vue` 的实时协议接线。
- 当前进度：协议 v1、WebSocket 路由、项目/Origin 校验、hello 握手、心跳、取消、最新帧背压和关闭清理已写入工作区；契约测试、前端类型回归与实际联验仍在进行。**尚未宣称 R0/R3 完成。**
- 其他 AI 可继续 R1/R2/R4/R5/R6 等独立任务；如要协助 R0/R3，请只新增独立契约/压力测试或提交问题清单，避免同时改网关实现。最新负责人和状态以[实时音视频任务表](C:/Users/h'h'h/.cursor/projects/d-WorkBuddy-rag-agent/canvases/realtime-video-task-plan.canvas.tsx)公告区为准。

## 2026-09-29 R4 真机联验（真 Key，只读探测）

- 用真实 `sk-ws-` Key 连 `wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-omni-turbo-realtime` 做了 5 轮只读探测（未产生有效对话费用）。
- **已验证可用**：建连与鉴权通过，服务端立即回 `session.created`。回显暴露了我原先的字段名错误并已改正：线上值是 `input_audio_format: "pcm16"` / `output_audio_format: "pcm24"`（不是 SDK 文档里的 `pcm_16000hz_mono_16bit` 长名）、`input_audio_transcription: {model: "gummy-realtime-v1"}`（不是布尔 `enable_input_audio_transcription`）、`turn_detection` 默认 `server_vad` + `threshold 0.5` + `silence_duration_ms 800`。
- **已验证不可用（硬事实）**：`input_audio_buffer.commit` 与 `response.cancel` 一发就被服务端**断开连接**（10054）。因此①`commit()` 改为直接拒绝并发出 `commit_unsupported` 错误事件，**不再发出任何报文**；②`interrupt()` 删除 `response.cancel`，只保留 `input_audio_buffer.clear`；③该端点实际只支持 **server_vad 自动断句**，Manual 模式不可用。
- **仍未证实**：`session.update` 不返回 `session.updated`（只回空帧），无法确认配置是否生效；1 秒 440Hz 纯音未触发 VAD 与任何响应事件（`response.create` 也不报错但无输出），说明**需要真实人声**才能完成端到端验证 —— 属 R12 真设备联验，不在本轮范围。
- 代码同步：常量改 `pcm16`/`pcm24`，新增 `TRANSCRIPTION_MODEL`；`availability()` 的 `verified` 由 `False` 改为 `connect-only` 并附原因；测试 22 → **24 项全绿**（新增 `commit` 被拒不得发出报文、`interrupt` 不得发 `response.cancel`）。

## 2026-09-29 R4/R5：实时模型适配器接口 + 统一时间线（AI-D）

- 原生实时音视频升级按任务表分工：R0（协议）与 R3（网关）由另一 AI 负责；本轮只落地 **R4 原生实时模型适配器** 与 **R5 实时多模态时间线**，且**未触碰** `agent_runtime/realtime_protocol.py`、`frontend/src/workbench/realtimeProtocol.ts`、`api.py` 网关、`AutonomousCockpit.vue`、`voice.py`。
- **新增 `agent_runtime/realtime_provider.py`**：provider 抽象层。适配器注册工厂、按 `DOCMIND_REALTIME_PROVIDER` 解析；归一化事件 `RealtimeEvent`（status/observation/transcript/text_delta/audio_delta/done/error）经 `to_wire()` 单点映射到 R0 的服务端事件名（协议升级只改这一处）。`resolve()` 在**未配置 / 未注册 / 无 Key** 时统一返回 `ok:False` + `reason` + `degraded_to: sampled-frames`，上层明确降级到现有抽帧链路，不伪造会话。
- **新增 `agent_runtime/realtime_omni.py`**：DashScope Qwen-Omni Realtime 适配器（`dashscope_omni`）。用已装的 `websocket-client` 直连 `wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=...`（Bearer `DOCMIND_OMNI_API_KEY`，回退 `DASHSCOPE_API_KEY`），**不新增依赖、不用 DashScope SDK**。默认 `qwen-omni-turbo-realtime`/音色 Chelsie/server_vad，`DOCMIND_OMNI_VAD=manual` 走 commit+create。音频 16k PCM16、视频帧走 `input_image_buffer.append`；单分片 >1MiB 直接拒绝并报错。
- **新增 `agent_runtime/realtime_timeline.py`**：R5 时间线。帧/观察/转写/回答绑同一 `captured_at` 单调时钟；**跨项目条目入场即丢弃**并计数；`evidence_for_speech()` 只返回不晚于「发言结束 + tolerance(1500ms)」的画面，没有可信证据时返回 `within:False` + 原因，供上层明说「无法从画面确认」。切项目 `set_project()` 全清。
- 验证：新增 `tests/test_realtime_provider.py` **22 项全绿**（假 WS 注入，不依赖真实 Key），覆盖降级三态、事件映射（含 Omni 的 audio_transcript.delta / audio.delta / input_audio_transcription.completed）、VAD/Manual 两种会话、超长音频拒绝、打断清缓冲、时间线的跨项目丢弃与「不晚于发言」约束。
- **未完成（不谎报）**：真实 DashScope Key 尚未开通，Omni 的 `session.*` 原始字段名与 `response.cancel` 事件名**未经真机联验**，已分别隔离在 `_session_payload()` 与 `interrupt()` 内，拿到 Key 后单点修正；真机联验属 R12。`.env.example` 本轮未改（另一 AI 正在改该文件），配置项见下。
- 配置：`DOCMIND_REALTIME_PROVIDER=dashscope_omni`、`DOCMIND_OMNI_URL`、`DOCMIND_OMNI_MODEL`、`DOCMIND_OMNI_API_KEY`、`DOCMIND_OMNI_VOICE`、`DOCMIND_OMNI_VAD`(server_vad|semantic_vad|manual)、`DOCMIND_OMNI_INSTRUCTIONS`。

## 2026-09-29 用户纠偏：自主开发舱实时视频流

- 用户明确要的是类似豆包的持续视频对话，不是桌面点击审核。开发舱现将屏幕共享与摄像头画面持续播放，并约每 500ms 编码一帧 JPEG 经项目绑定的 WebSocket 发送；画面采集与视觉模型推理分离，模型忙时服务端仅保留最新待分析帧，丢弃中间积压帧。每条观察携带原始采集时间，继续供语音发言时段匹配。
- WebSocket 拒绝未登记项目与外站 Origin，限制单帧大小；连接关闭后清除待处理帧，画面不写磁盘或聊天历史。模型不可用时本地画面仍播放并明确提示；临时断线按退避间隔自动重连，项目或 Origin 拒绝则停止重试。摄像头与屏幕共享可随时暂停或停止；桌面控件点击入口移入折叠的高级区，不再占据实时视频主入口。
- 验证：前端 typecheck/build 通过；后端持续视觉和语音测试 16 项通过，其中覆盖 WebSocket 项目/Origin 隔离、采集时间回传及模型忙时只处理最新帧。尚未做真实摄像头、屏幕共享和麦克风联验。当前视觉模型仍按采样帧理解，不是原生音视频同流的 Realtime 多模态模型；真实对话延迟取决于所选模型，项目预览/原生桌面入口仍沿用原来的逐帧分析链路。

## 2026-09-29 自主开发舱连续视觉操作轮次

- 当前项目的嵌入桌面窗口新增「按目标连续观察与复验」入口。用户写明可见目标和首个控件后，AI 定位控件，用户逐步确认点击，系统采集点击前后画面并复验；未达成且模型有新控件建议时自动定位下一步，再次等待用户确认。模型判断可能达成时等待用户验收；不确定、无可靠建议、定位失败或建议重复点击已尝试控件时暂停。
- 每一步保留控件、模型判断和可见证据；轮次按项目与工作流保存在本标签会话中，刷新后的旧点击提案一律失效并要求重新定位。有当前工作流时，复验未达成会沿用视觉反馈链路交给 Agent，可能达成的画面证据也保存到工作流反馈记录并等待用户确认。停止定位不会执行尚未确认的点击；执行中的点击不能被伪装为已取消。
- 验证：前端 typecheck/build 通过，连续视觉轮次与语音画面时间匹配测试 6 项、后端视觉定位/持续视觉测试 20 项通过。真实 Windows 多步操作和视觉模型联验尚待在用户设备进行。当前连续操作只覆盖项目嵌入桌面窗口的单次点击串联，输入、拖拽与网页 iframe 控件仍待统一接入。

## 2026-09-29 自主开发舱视觉与语音同步

- 持续视觉的每帧带采集时间，模型观察另带完成时间。语音识别以发言时段匹配当前项目的近邻帧；服务端转写在录音结束时冻结画面证据，主 Agent 忙时连同语音一起排队，避免转写或排队结束后误用新画面。
- 语音陪聊只接收该发言时段已完成的观察；无可信近邻画面时明确说明不可确认。主 Agent 的语音请求附固定截图、时间线和独立的内部时间关系提示，用户消息仍只显示用户原话。缓冲最多 20 帧，项目切换或停止观察时清空。
- 验证：前端 typecheck/build 通过；时间匹配测试 3 项、后端持续视觉/语音定向测试 12 项通过；`api.py` 编译与相关文件 `git diff --check` 通过。真实麦克风、屏幕共享、视觉模型和 TTS 延迟仍需在用户机器上联验。

## 2026-09-29 P0：命令执行预算与前端观察信号

- **根因**：`execution_mode()` 默认 `enterprise`，本机没有 docker，`run_command` 走容器路径必然 `SandboxUnavailable`——不是「超时 12s」，是根本跑不了。`agent_runtime/enterprise_sandbox.py` 新增 `container_available()` / `fallback_policy()` / `host_fallback_active()` / `run_bounded_host_command()`：只有**非 staged** 命令在 `DOCMIND_SANDBOX_FALLBACK=host`（默认）时才降级到宿主隔离，**staged 命令保持 fail-closed**（`test_enterprise_sandbox_review` 依赖此语义）。降级时 `tools._shell_argv` 改用 `cmd /c`。
- **新增 `agent_runtime/process_runner.py`**：`_CappedBuffer` 头尾双截（头尾各 4000 字符，中间省略明确标注，报错尾部不再被切掉）；`run_bounded` 超时可配（上限 300s）；`start_job` / `job_logs` / `job_cancel` / `job_count`（上限 8 个任务、后台超时上限 1800s、TTL 900s 清理）。子进程带 `PYTHONUNBUFFERED=1`，否则块缓冲日志在 cancel 时被丢光。`windows_sandbox.py` 新增 `spawn_isolated` / `terminate_isolated` 供后台监管杀整棵树。
- **`tools.py`**：`run_command` 支持换行追加 `timeout: <秒>` 与 `background: true`（首行不解析选项，避免误判）；新增 `dev_job_logs` / `dev_job_cancel`（也接受裸 id）。`python_exec` 12s→60s 且输出改窗口化（原 `[:1500]` 单向截断会把 traceback 切掉）；`dev_region_verify` 同步。工具数 86→88。
- **P0-2 `visual_acceptance.py`**：`_DevTools` 改为事件收集器，采集 `Runtime.consoleAPICalled` / `Log.entryAdded` / `Network.responseReceived(>=400)` / `Network.loadingFailed` / `Runtime.exceptionThrown`，新增 `drain()`；`checks` 改为 `page_loaded / no_runtime_errors / no_console_errors / no_failed_requests`，任一不满足即 `passed=False`，与截图同权。`_QuietHandler` 对 `/favicon.ico` 返回 204，否则每次预览都会因浏览器自动探测而误判失败。
- **验证**：真机 Edge headless 实测——脏页面 `console_errors=2` / `failed_requests=1` / `passed=False`；干净页面 `passed=True`。新增 `tests/test_command_execution.py` + `tests/test_visual_console_signals.py` **43 passed**。全量 `unittest discover` **1763 tests / 5 failed / 6 skipped**，5 个失败经 worktree 对照二分确认全部来自另一 AI 的未提交改动（`api.py` 删了 notice/心跳 yield、`agent.py` 348 行改动），与本轮无关。

## 2026-09-29 P1：改→看→找 闭环（`dev_git_diff` / `dev_find_references` / `dev_apply_edits`）

- 核心逻辑放在**新模块 `agent_runtime/code_intel.py`**（`tools.py` 已 6300 行，且是交叠文件，薄封装能降低与另一 AI 的冲突面）；`tools.py` 只做参数解析 + 注册。工具数 88→**91**。
- **`dev_git_diff`**（只读，绝不写入/暂存/提交）：`status --porcelain` 清单（区分已暂存/未暂存/未跟踪）+ `--stat` + 正文，支持 `paths/staged/stat/context/timeout`。走 `process_runner.run_bounded` 有界执行；paths 校验越界与 `-` 开头 flag 注入；正文过长窗口化并标注。未跟踪新文件不在 diff 里，会单独提示。
- **`dev_find_references`**：Python 走 **ast** 精确匹配（注释与字符串里的同名文本不误报）；其他语言词边界正则 + 跳纯注释行；`TOOLS` 注册表那种字符串键命中单列为 `string_hits` 低置信类（重命名时它也得改，纯 AST 会漏）。`scope` 无效时报错，不静默退化成全仓扫描。
- **`dev_apply_edits`**：多文件批量编辑，**原子**——全量预检（越界/分区写/old_text 唯一匹配/200KB/.py 语法）通过才落盘，任一失败「一个文件都不写」；落盘中出错回滚已写文件。`---` 分块，new_text 内含 `---`（Markdown 分隔线）时回合并上一块，避免误切损坏内容。
- **三个真机坑**：① git 的 stderr warning 被合并进 stdout 后污染 status 解析（清单里出现 `w ning: ...`），已加 `_strip_git_noise` + porcelain XY 严格校验；② 中文路径被 git 转义成八进制，已加 `-c core.quotepath=false`；③ `dev_apply_edits` 会绕过写后自验证收尾门，`agent._parse_written_rel` 已加 `已原子写入` 多路径分支，并同步进 `_NO_PARALLEL_TOOLS` / `_WRITE_TOOLS` / 自验证触发元组。
- **验证**：新增 `tests/test_agent_code_tools.py` **38 passed**；相关回归 148 passed。全量 `unittest discover` = **1799 tests / 5 failed / 2 errors / 6 skipped**，**7 个红项全部来自另一 AI 的未提交 WIP**：2 errors 是 `test_cloud_agent_registration` 没跟上对方给 `api.chat` 新增的必填 `request: Request` 参数（api.py 有 863 行对方新增）；5 failures 同 P0 已归因的那批。

## 2026-09-29 桌面复验回传自主开发任务

- 已确认的桌面点击若视觉复验为 `unmet` 或 `uncertain`，开发舱会将目标、可见证据和前后截图送回当前自主开发任务：建立持久视觉反馈记录，以点击后画面作为 Agent 本轮修改前快照，并在 Agent 回复后再次捕获项目嵌入窗口作效果对比。无当前任务时保留本地复验显示并提示先启动任务。
- 回传走开发舱专属对话活动，工作台自动生成的资料不会冒充用户发言。后端要求反馈编号已登记且属于当前项目；模型产生的证据保留为低信任资料，不提升为 system 指令。Agent 先核对项目/画面，文件修改和新的桌面动作继续经过现有审批门，最终效果由用户验收。
- 对话忙或审批门打开时最多排队 5 条自动反馈；工作流仍在执行时先保存记录，待工作流退出执行态再交给对话 Agent，避免并发修改同一项目。队列满、切项目或进程重启后仍可从视觉反馈记录手动重试。反馈完成后的原生窗口截图要求严格项目绑定。
- 验证：相关后端定向测试 70 项通过，前端 typecheck/build 通过。尚未做真实 Windows/视觉模型联验；本轮是反馈账本与开发舱对话联动，运行中的工作流执行器不会在同一波次直接读取新反馈。

## 2026-09-29 桌面点击后的模型视觉复验

- 用户可填写期望的可见效果；确认点击后，后端以点击前最后一帧和点击后同一窗口的画面交给视觉模型比较，返回 `met / unmet / uncertain / unavailable`、可见证据及可选的下一控件建议。无画面、画面不变、窗口变化、模型异常或无可靠 JSON 时不会宣称达成。
- 自主开发舱展示点击前后截图和视觉复验结论。若模型认为未达成且指出下一控件，前端自动重新定位并展示新的目标框；每次实际点击仍需用户逐次确认，模型建议不会直接触发输入事件。复验只判断画面可见结果，不能证明文件、保存或内部软件状态。
- 验证：后端视觉相关定向测试 64 项通过，前端 typecheck/build 通过。真实视觉模型判断质量、Windows 点击与较慢界面的截图时机仍需真机联验。

## 2026-09-29 自主开发舱视觉定位与单次点击

- 预览区可输入控件描述，后端从当前项目已登记的嵌入 Windows 窗口截图，让视觉模型返回目标框与客户区坐标；前端叠加目标框供用户核对。
- 用户确认后，后端一次性消耗 45 秒有效的点击提案，复查项目、窗口、尺寸和控件附近画面，记录审批，执行一次点击并重新截图。窗口切换或画面变化时拒绝执行；项目没有专属嵌入宿主时不会回退到其他窗口。
- 目前只覆盖嵌入窗口的单次点击；网页 iframe、屏幕共享坐标、输入、拖拽、操作后的模型视觉复验与自动循环尚未接入。真实 Windows 点击和视觉模型定位精度尚待真机联验。
- 定向测试 61 项通过，前端 typecheck/build 通过。构建仍有既有的非 module 脚本和大 chunk 提示。

## 2026-09-29 自主开发舱视觉变化、重点区域和异常提醒

- 持续视觉在最近五次观察中保留时间线，下一条开发舱对话和语音陪聊可使用最近变化；前后帧比较由视觉模型执行，画面只作为未核实资料进入主 Agent 上下文。停止/切项目时清理帧、请求和时间线。
- 前端以缩小后的 RGB 画面过滤静止及压缩噪声，局部或颜色变化会触发分析，静止画面约 20 秒复查；可在快照上拖拽圈选重点区域，模型分析裁剪画面，开发舱聊天同时获得整屏与选区图。
- 视觉模型可返回结构化异常线索；后端校验类型、可见目标、证据和置信度。前端要求下一帧出现相同目标才显示“疑似异常”提醒与截图，避免单帧结论直接当成故障。用户点击“交给 AI 排查”才发送截图与核实任务；修改前仍需确认。
- 验证：前端 typecheck/build 通过，视觉相关前端测试 6 项、后端定向测试 20 项通过。真实屏幕共享、模型 JSON 遵循程度、假阳性率和语音同步尚需真机联验；当前仍是视频播放加抽帧分析。

## 2026-09-29 自主开发舱持续视觉观察

- 自主开发舱增加「观察项目画面」和「共享屏幕给 AI」两种持续视觉入口。前者读取当前网页预览或项目嵌入窗口，后者使用浏览器 `getDisplayMedia` 让用户选择窗口/屏幕；共享画面在开发舱中实时播放，可随时停止。
- 视觉模型按帧采样，不把整段视频逐帧塞进主对话。前端串行采样、窗口隐藏时暂停、相同画面跳过分析；后端 `/api/vision/frame` 按项目限流且同项目只允许一帧分析。原生视觉模型直接分析，文本模型使用已配置的 Harness 视觉模型；缺模型时给出明确提示。
- 最新帧进入自主开发舱聊天的下一条消息，语音协作代理也能取得最近的画面观察文字，因此“这里不对”等语音反馈可以结合当前画面交给主 Agent。停止共享或切换项目后清除帧。
- 原生桌面帧由 `/api/vision/desktop-frame` 在内存中编码，不会因持续观察反复往项目 `.docmind/screenshots` 写文件；只允许项目嵌入窗口或明确的前台窗口目标。
- 验证：前端类型检查与生产构建通过；持续视觉、截图、桌面适配器和运行时契约定向测试 51 项通过（另有 4 个 subtests）。真正屏幕共享和视觉模型延迟仍需在用户机器上联验；当前实现是实时画面播放加串行抽帧理解，分析频率取决于模型推理耗时。

## 2026-09-29 自主开发舱主动视觉观察入口

- 自主开发舱预览区新增“AI 观察当前界面”：网页实时预览会自动截取当前 iframe，并作为图片随当前项目对话发送给视觉模型；模型回复显示在自主开发舱聊天区。
- 没有网页预览时，入口仍会把“浏览当前界面”意图交给 Agent，系统提示要求优先调用 `dev_desktop_capture` 获取当前项目嵌入窗口或前台窗口，捕获失败时必须如实说明。
- 引擎与场景运行面板同样增加“让 AI 观察当前画面”，网页试玩直接附图，原生窗口交给桌面视觉工具；原有“截图并反馈给 AI”修改反馈流程保留。
- 观察状态会显示读取中、AI 观察中、完成、暂存或失败，不会伪造已看过画面；本次仅涉及前端入口和 Agent 视觉触发提示。
- 修正运行时工具元数据：`dev_desktop_capture` 是只读观察工具，归类为 `read_local/pure`，不会因用户只要求查看画面而触发写操作拦截；增加对应注册回归断言。
- 验证：前端类型检查与生产构建通过；截图、桌面适配器和运行时契约定向测试 46 项通过（另有 4 个 subtests）。构建仅有既有非 module 脚本和大 chunk 提示；pytest 仍有既有缓存目录权限提示。

## 2026-09-29 窗口尺寸护栏与自主开发舱语音范围

- 修复模型设置等弹窗在短窗口中底部被裁切的问题：统一弹窗采用 `border-box`、视口内最大高度、遮罩滚动和内部滚动；模型设置移动端改为视口内贴底面板。
- 检查并补强通用 `.wb-modal-shell` 尺寸护栏，避免其他使用公共弹窗类的设置、审批、历史和预览面板因 padding 叠加超出窗口。
- 语音按钮和语音状态请求限定在自主开发舱 `ChatDock scope="cockpit"`；普通代码工作台对话不再显示或初始化语音识别、语音协作和 TTS。

## 2026-09-29 长期记忆、无限工作流预算与语音协作

- Agent 普通回合、子 Agent 和自主开发工作流统一支持 `0 = 不设固定工具步数上限`。工作流的 LangGraph 波次、重试、原生调度和重规划都已区分“无限”与“预算耗尽”；取消、连续失败、重复调用、审批、费用预算和单轮 deadline 仍可停止任务。若部署侧设置 `DOCMIND_WORKFLOW_STEPS_MIN/MAX` 为正数，可恢复工作流预算护栏。
- `agent_memory.py` 的 SQLite 长期记忆新增 `user_profiles` 表和画像读写；画像只接受用户明确填写或第一人称声明。API 提供 `/api/agent/profile`、`/api/agent/memory`，设置页的“智能体”面板增加画像字段、目标、备注和项目记忆删除入口。
- 新增可选 OpenAI 兼容 STT/TTS 桥接（`voice.py`），未配置服务端时使用浏览器语音识别/合成；`/api/voice/status`、`/api/voice/transcribe`、`/api/voice/speech` 和无工具 `/api/voice/dialogue` 已接入。对话台在主 Agent 忙时调用语音协作 Agent 陪聊；“这里不对 / 改一下 / 继续”等反馈会排队交给主 Agent，主 Agent 输出按句子片段进入 TTS 或浏览器语音队列。
- 联网搜索已按多来源并行扇出，默认搜索引擎扇出 6、社区/补充来源扇出 8，覆盖 DuckDuckGo、百度、Bing、GitHub、B 站、知乎、百度贴吧、小红书、微博、CSDN、Stack Overflow；后续可继续增强搜索结果驱动的评价/做法/重新发现循环。
- 离线验证：`tests/test_agent_memory.py tests/test_voice.py` 共 19 项通过；前端 `npm run typecheck` 与 `npm run build` 通过。旧 `tests/test_workflow_step_budget.py` 中仍有 24/200 旧预算断言，需按新的无限语义更新测试期望。

## 2026-09-29 工作台文件预览与 Harness 二进制检查

- 工作台现在可直接打开 `.gitignore` 等常见无扩展名配置文本，并按原有保存护栏编辑。`.scn`、`.bin` 和其他不可编辑的二进制文件改为只读预览，显示类型、大小、SHA-256、文件头、十六进制和可见字符串；大文件只读显示前段，编辑器与保存接口均不覆盖原始字节。
- `inspect_data_file` 与 `read_file` 会识别二进制，不再用忽略解码错误的方式丢失字节。`generate_data_file` 可生成 `.gitignore` 文本或由明确 base64/hex 字节指定的 `.bin`；`.scn` 必须带 Godot 资源头并回读核对字节，是否能在 Godot 加载仍需引擎验证。一般场景应生成 `.tscn` 源文件，由 Godot 生成缓存。
- 定向回归覆盖工作台打开/禁止二进制保存、Harness 二进制检查、字节生成和 `.gitignore` 生成：相关 86 项通过；前端 typecheck/build 通过。

## 2026-09-29 Harness 长回答与工具步数

- 代码审查记录显示旧默认工具预算为普通 8 步、代码 12 步、项目审查 16 步，导致尚未核完源码就强制收尾。现提高为 64/96/128 步；子代理默认/硬顶提高为 24/64 步，均可由原环境变量覆盖。当前 `.env` 里显式的旧 8 步也已同步调高，`.env.example` 的预算与观察长度示例已更新。重复调用、连续失败、取消与权限护栏仍生效。
- `finish_reason=length` 且已开始 `Final Answer` 时，保留已生成正文并续写，最多生成 12 段；仍未结束则展示已有正文并标为未完成，避免把半段答案或压缩重写当作完整结论。纠偏类提示不再作为“复核与重试”步骤占据工具时间线。
- 更新 `read_file` 提示与工具描述中的旧 4000/1200 字说明，匹配实际默认前 12000 字及按行读取能力。前端将剩余反思事件标为“执行提示”。
- 离线回归覆盖 20 步代码审查、连续长度截断接续、重复片段合并和未完成标记；此项尚未进行真实云端模型联验。

## 2026-09-28 概览页主布局重做

- 根据实际工作台截图调整 `OverviewView.vue`：扩大概览内容宽度，项目状态改为清晰的摘要卡，右侧上手/最近打开/提问区域改为带层次的辅助卡片并在桌面端保持可见。
- 调整 `RegionHomeView.vue`：分区卡片提高信息密度、尺寸和悬停层次，减少主区大片空白造成的“未优化”观感；保留分区定位、刷新和治理入口逻辑。
- 调整 `WorkspaceTabs.vue`：提高非活动工作区标签的可读性，仍保持无代码标签时的禁用状态。
- `ChatDock.vue` 的工具调用时间线改为“类别标签 + 工具名 + 摘要 + 状态胶囊 + 可展开事件详情”，并补充 `aria-expanded` 和移动端单列布局；不改变 trace 数据、工具调用或流式管线。
- `App.vue` 将顶部重复的“AI 问答”页面链接改为聚焦现有底部对话台的“AI 对话”入口，避免打开第二套聊天页面。
- 顶栏层级收敛：`AI 对话`作为唯一主操作使用主色，`设置`和低频菜单恢复中性样式，避免多个蓝色按钮同时争夺注意力；不改变菜单动作和面板入口。

## 2026-09-28 代码区与运行台交互细化

- `style.css` 为代码头部、编辑器标签栏和关闭按钮补充一致的悬停、按压、焦点反馈，并限制长面包屑溢出。
- 为代码区和 Harness 运行台增加 760px/520px 窄屏布局；运行台统计、预算表单、轨迹信息在小窗口下改为可读的单列节奏。
- 为 Harness 弹层、列表项补充轻量入场动效，并统一支持 `prefers-reduced-motion`；不改变会话、预算、轨迹或技能业务逻辑。
- `EditorTabs.vue` 的关闭标签控件补充 Enter/Space 键盘操作，保留鼠标与中键关闭行为。
- `WorkspaceTabs.vue` 补充工作区标签焦点/按压反馈与减弱动效支持；`SceneFileCard.vue` 允许已解析文件卡通过键盘打开，未改变图谱选择与双击行为。
- `AppDialog.vue` 为确认、提醒和文件冲突弹窗设置稳定的初始焦点，并为窄屏操作按钮增加换行布局；输入弹窗仍自动选中文件名主体。
- `ChatComposer.vue` 为联网搜索和深度思考开关补充按压语义、发送/停止按钮补充操作提示，并加入 560px 以下输入区布局；不改变消息发送、附件处理或停止请求逻辑。
- `ChatDock.vue` 为折叠热区、会话/工作流按钮补充控制关系和按压语义，窄屏下收紧头部并让孤儿工作流恢复条换行；不改变会话、工作流或流式输出逻辑。

## 2026-09-28 代码导航区域视觉优化

- `SymbolMap.vue` 增加代码地图弹层入场、筛选/文件/符号按钮焦点与按压反馈，并完善窄屏工具栏和内容布局。
- `SymbolOutline.vue` 增加大纲收起、展开和符号定位的焦点反馈，小屏下收窄侧栏宽度。
- `style.css` 为文件树行和新建/刷新按钮补充悬停、焦点和轻微定位反馈；未改变索引、筛选、定位或文件树展开逻辑。

## 2026-09-28 资源预览状态优化

- `ModelPreview.vue` 增强 3D 预览容器层次、加载遮罩和失败提示，加载失败时“仍可导入”的状态更明确。
- `SpritePlayer.vue` 为帧动画画布增加容器边界、阴影和失败提示样式，预览区域更容易与周围素材区分。
- 支持 `prefers-reduced-motion`，仅调整展示层，未改变模型加载、动画播放或资源导入逻辑。

## 2026-09-28 确认弹窗与差异预览优化

- `RewriteDiffDialog.vue` 增加差异预览弹层入场、恢复/取消按钮焦点与按压反馈，以及窄屏按钮并排和统计栏换行布局。
- `style.css` 为通用确认/输入弹窗补充焦点态、按压反馈和减少动态效果支持。
- 仅调整展示层、动效和响应式样式，未改变差异计算、改写接受、文件保存或确认结果逻辑。

## 2026-09-28 场景节点与引用卡片交互优化

- `SceneCanvas.vue` 为场景引用、面包屑、属性关闭、折叠按钮和 Vue Flow 控件补充键盘焦点与按压反馈。
- 节点/文件卡片和折叠按钮在悬停、选中和减少动态效果模式下的状态更一致；未改变场景图布局、节点折叠、文件打开或关系高亮逻辑。

## 2026-09-28 项目概览与分区首页视觉优化

- `OverviewView.vue` 为上手清单、最近文件、快捷提问、空状态和恢复入口补充焦点/按压反馈，并完善窄屏布局。
- `RegionHomeView.vue` 增强分区卡片悬停、定位提示、刷新与缺失分区操作反馈；支持 `prefers-reduced-motion`。
- 仅调整展示层、动效和响应式样式，未改变文件打开、分区定位、治理跳转或 AI 提问逻辑。

## 2026-09-28 选区 AI 与右键菜单交互优化

- `SelectionToolbar.vue` 增加选区操作按钮焦点/按压反馈和窄屏换行布局，解释、Review、改写、提问的动作更容易确认。
- `SelectionAiPanel.vue` 增加结果面板和回答卡片入场、停止/发送/替换/复制按钮反馈；小屏改为底部面板，保留流式滚动体验。
- `style.css` 补充右键菜单项目焦点与过渡效果；支持 `prefers-reduced-motion`，未改变 AI 调用、改写替换或右键菜单命令逻辑。

## 2026-09-28 设置与历史弹窗视觉优化

- `SessionHistoryPopover.vue` 增加遮罩、弹层入场、会话条目悬停与键盘焦点反馈；窄屏改为贴边全高弹层，删除和新建操作更容易确认。
- `GitHistoryDialog.vue` 增加历史弹窗入场、提交条目选择反馈、恢复按钮焦点态与移动端上下布局；支持 `prefers-reduced-motion`。
- 仅调整展示层、动效和响应式样式，未改变会话切换、删除、历史预览或版本恢复逻辑。

## 2026-09-28 AI 权限与 GPU 面板视觉优化

- `AgentPolicyPanel.vue` 增加授权弹层遮罩、审批卡片层次、按钮/输入框焦点反馈和窄屏全高布局，外部文件授权与工具动作审批状态更清楚。
- `GpuPanel.vue` 增加 GPU 弹层入场、显卡卡片悬停、显存条平滑变化、危险操作反馈和移动端布局；支持 `prefers-reduced-motion`。
- 仅调整展示层、动效和响应式样式，未改变权限审批、GPU 租约回收、排队取消或 Ollama 卸载设置逻辑。

## 2026-09-28 自主执行模型栏视觉优化

- `CockpitModelBar.vue` 增加模型状态层次、当前生效模式徽章、焦点态和按压反馈，自动选模不可用或切换失败时更容易识别原因。
- 模型栏在 760px 以下改为多行布局，预设选择和状态说明保持可读；支持 `prefers-reduced-motion`。
- 仅调整展示层、动效和响应式样式，未改变模型模式切换、预设保存、自动选模或错误回滚逻辑。

## 2026-09-28 引擎连接与任务生成面板视觉优化

- `EngineConnectPopover.vue` 增加遮罩与弹层入场、连接器状态卡片悬停、按钮焦点/按压反馈；窄屏下连接器操作自动换行并改为贴边面板。
- `TaskEnginePanel.vue` 扩大复杂任务面板的可用宽度，增强任务结果、引擎状态、生成输出和错误入口的层次与动效；小屏改为全高面板，并支持 `prefers-reduced-motion`。
- 仅调整展示层、动效和响应式样式，未改变 MCP 探活、引擎控制、ComfyUI 生成、任务保存或导入逻辑。

## 2026-09-28 素材中心交互反馈优化

- `AssetCenterView.vue` 为来源/子页签、筛选、素材卡片、抽屉和导入按钮补充键盘焦点态与弹层入场动画，素材包菜单、详情抽屉和预览弹窗打开时反馈更清晰。
- 增加 `prefers-reduced-motion` 处理；仅调整展示层和动效，未改变素材搜索、下载、预览、导入或素材库逻辑。

## 2026-09-28 分区可视化弹窗交互优化

- `RegionMapDialog.vue` 增加弹层入场、契约状态强调、分区卡片悬停/按压和按钮焦点反馈，新增分区与刷新等操作的状态更容易识别。
- 分区图在 760px/520px 以下改为单列和紧凑顶部操作，项目根路径和卡片内容在小屏保持可读；支持 `prefers-reduced-motion`。
- 仅调整展示层、动画和响应式样式，未改变分区创建、导出补全、契约校验或 AI 规划逻辑。

## 2026-09-28 关系图与 Unity 引用图视觉优化

- `RelationGraph.vue` 与 `UnityGraph.vue` 增加弹层入场动画、按钮/筛选项焦点态、节点过渡和按压反馈，让缩放、过滤、查看详情等操作更容易确认。
- 两个图谱窗口在 900px/620px 以下自动收紧工具栏；Unity 详情栏在窄屏改为画布下方区域，关系图详情卡和图例支持窄屏滚动。
- 支持 `prefers-reduced-motion`，仅调整展示层与响应式样式，未改变图数据加载、力导向布局、筛选、拖拽、定位或文件打开逻辑。

## 2026-09-28 工作流与运行预览视觉反馈优化

- `WorkflowCard.vue` 增加任务完成进度条、阶段完成连线和执行状态脉冲；任务、成员、按钮、复核区增加悬停与状态层次，失败/通过结果更易区分。
- `SceneRuntimePanel.vue` 增加运行预览弹层和画面反馈的入场动画、按钮与标签反馈、事件时间线和 Bug 卡片高亮；运行预览在 900px/560px 以下自动改为单列和紧凑工具栏。
- 两个组件均支持 `prefers-reduced-motion`，保留现有执行、审批、截图和回滚逻辑不变。
- 验证：前端 `npm run typecheck`、`npm run build`、`git diff --check` 通过。构建仅保留既有 `/static/session.js` 非 module 和大 chunk 提示。

## 2026-09-28 审核队列与适配器面板视觉优化

- `CockpitApprovalQueue.vue` 为风险等级、待处理卡片、审核弹窗和操作按钮增加颜色层次、悬停/焦点反馈、入场动画与窄屏布局。
- `AdapterManagerPanel.vue` 优化适配器状态徽章、代码差异、真实验收报告和按文件回滚区域的层次与交互反馈。
- 两个组件继续支持 `prefers-reduced-motion`，未改变审批、适配器激活或回滚逻辑。
- 验证：前端 `npm run typecheck`、`npm run build`、`git diff --check` 通过；构建警告仍为既有脚本类型和大 chunk 提示。

## 2026-09-28 视觉反馈历史与预览状态优化

- `WorkflowPreview.vue` 为文件变化、实时预览、反馈弹窗和历史反馈增加层次、状态徽章、入场/悬停反馈与键盘焦点样式。
- 反馈状态按待发送、处理中、等待检查、已确认、需继续修改和失败区分颜色；截图对比和移动端工具栏更易操作。
- 仅调整展示层与动画，未改变截图、反馈发送、重试和验收状态逻辑。
- 验证：前端 `npm run typecheck`、`npm run build`、`git diff --check` 通过；构建警告仍为既有脚本类型和大 chunk 提示。

## 2026-09-28 工作流审批弹窗视觉优化

- `WorkflowGateDialog.vue` 突出推荐方案、联网标记、任务条目和高风险操作；长内容滚动时底部审批操作保持可见。
- 审批弹窗增加焦点、悬停、入场动画和输入框反馈；窄屏改为贴底审核面板，并支持 `prefers-reduced-motion`。
- 仅调整展示与交互反馈，未改变方案选择、计划审批、验收条件或任务重排逻辑。
- 验证：前端 `npm run typecheck`、`npm run build`、`git diff --check` 通过；构建警告仍为既有脚本类型和大 chunk 提示。

## 2026-09-28 对话区长响应反馈优化

- `ChatDock.vue` 增加运行状态脉冲、思考/错误状态卡片、工具活动行焦点反馈和弹层入场动画。
- 快捷提问、继续执行、跳到底部和工具详情在悬停与窄屏下更易操作；保留停止、恢复和审批锁定逻辑。
- 支持 `prefers-reduced-motion`，未改变消息流、SSE、工具调用或会话恢复逻辑。
- 验证：前端 `npm run typecheck`、`npm run build`、`git diff --check` 通过；构建警告仍为既有脚本类型和大 chunk 提示。

## 2026-09-28 工作流历史与成员浮层优化

- `WorkflowHistoryPopover.vue` 优化历史条目、状态圆点、恢复/中断/删除按钮和小屏弹层反馈。
- `WorkflowMembersDock.vue` 优化团队成员浮层、状态徽章、悬停和键盘焦点反馈。
- 仅调整展示层与动画，未改变历史恢复、删除、中断或成员定位逻辑。
- 验证：前端 `npm run typecheck`、`npm run build`、`git diff --check` 通过；构建警告仍为既有脚本类型和大 chunk 提示。

## 2026-09-28 场景画布视觉与响应式优化

- `SceneCanvas.vue` 增加工具栏按钮、路径输入、画布焦点、属性输入和引用文件的交互反馈。
- 画布与属性栏增加层次阴影；900px 以下改为上下布局，560px 以下工具栏和搜索框自动收紧。
- 仅调整展示层和响应式样式，未改变节点编辑、撤销重做、导出或场景数据逻辑。
- 验证：前端 `npm run typecheck`、`npm run build`、`git diff --check` 通过；构建警告仍为既有脚本类型和大 chunk 提示。

## 2026-09-28 运行时时间线视觉与响应式优化

- `RuntimeTimeline.vue` 优化筛选胶囊、缩放/刷新/导出按钮、事件点和详情栏的交互反馈。
- 时间轴与事件详情增加层次阴影；800px 以下改为上下布局，移动端筛选区域可滚动。
- 仅调整展示层和响应式样式，未改变事件采集、过滤、指标曲线、导出或清空逻辑。
- 验证：前端 `npm run typecheck`、`npm run build`、`git diff --check` 通过；构建警告仍为既有脚本类型和大 chunk 提示。

## 2026-09-28 AI 流程画布视觉优化

- `FlowCanvas.vue` 增加流程弹层入场、回合列表、节点卡片和工具按钮的悬停/焦点反馈。
- 小屏幕下压缩回合列表与统计标签，保留画布、节点详情和错误状态的可读性。
- 支持 `prefers-reduced-motion`，未改变流程数据、节点选择、工具追踪或详情逻辑。
- 验证：前端 `npm run typecheck`、`npm run build`、`git diff --check` 通过；构建警告仍为既有脚本类型和大 chunk 提示。

## 2026-09-28 模型设置弹窗视觉优化

- `ModelSettingsDialog.vue` 优化模型模式、能力标签、上下文探测、预设列表和高权限模式的视觉层次。
- 输入框、模式选择、预设操作和保存按钮增加焦点/悬停反馈；小屏改为贴底配置面板，底部操作保持可见。
- 支持 `prefers-reduced-motion`，未改变模型探活、上下文识别、凭据保存或预设切换逻辑。
- 验证：前端 `npm run typecheck`、`npm run build`、`git diff --check` 通过；构建警告仍为既有脚本类型和大 chunk 提示。

## 2026-09-28 前端工作台视觉优化

- `frontend/src/workbench/style.css` 增加统一的层次、焦点态、按钮反馈、面板入场和减少动态效果策略；顶栏、文件树、编辑器和状态栏的背景与阴影更有层次。
- `WorkspaceTabs.vue` 优化工作区标签的激活指示、悬停位移和渐变底线动画。
- `AutonomousCockpit.vue` 优化开发舱左右布局、卡片阴影、实时预览、空状态、反馈弹层和按钮动效；宽度较小时继续自动收窄为单列。
- `ChatDock.vue` 与 `ChatComposer.vue` 补充消息渐入、工具活动流高亮、输入区聚焦、发送按钮和附件反馈动效，普通聊天与开发舱聊天保持同一套交互节奏。
- 顶部代码图/工具菜单、项目菜单和启动等待态增加轻量弹出、遮罩和加载反馈，减少“点击后没有反应”的视觉误判。
- 验证：`npm run typecheck` 通过，`npm run build` 通过。构建仅保留既有 `/static/session.js` 非 module 和大 chunk 警告。

## 2026-09-28 通用文件识别、脏数据处理与项目数据生成

- 新增 `data_formats.py` 作为统一数据层：按扩展名与 magic bytes 识别真实格式，探测 UTF-8/UTF-16/GB18030 等编码；支持脏 CSV/TSV（分隔符探测、重复表头改名、短行补空、空值/数字/布尔归一化）、JSON/JSONL（坏行单独报告）、YAML、TOML、XML、HTML，以及 PDF/DOCX/XLSX/PPTX 文本抽取。
- 上传 `/api/ingest` 现在返回 `detected` 格式、MIME、编码、大小和识别置信度；文档摄取与代码索引共用新的解码/抽取链路，避免把 GB18030、BOM 或脏表格当乱码。
- Agent 新增 `inspect_data_file` 和 `generate_data_file`。前者返回受限的结构化检查结果；后者在当前项目内原子生成并回读校验 CSV/TSV/JSON/JSONL/YAML/TOML/XML/TXT/MD，默认不覆盖已有文件。Word/PDF/PowerPoint/Excel 继续使用 `create_artifact`。
- 生成数据文件属于项目写操作，加入并发互斥、试做区工具白名单和写权限元数据；路径越界、覆盖、大小超限和无法回读都会失败。
- 新增 `tests/test_data_formats.py`；数据层、Agent、原生工具和 API 定向测试通过，前端 typecheck 通过。

### 本轮补强

- 增加无 BOM UTF-16 的启发式识别，避免把带 NUL 字节的文本误判为 latin-1/乱码。
- JSON、YAML、TOML、XML 解析失败时返回原文预览和结构化 `errors`，脏文件可以继续进入检查和人工修复流程，不会直接让整次摄取崩溃。
- `generate_data_file` 先在带目标扩展名的临时文件中回读校验，校验通过后才原子替换目标；不可解析的生成结果不会留下半成品。
- 数据格式定向测试现为 7 项；`tests/test_agent.py tests/test_api_routes.py tests/test_data_formats.py` 共 59 项通过。

### 测试基线收尾

- 修正上下文压缩回归测试，使其检查压缩通知是否出现，不再假设压缩通知必须是第一条；搜索测试关闭缓存并覆盖 GitHub 专用入口的降级路径，适配多来源聚合返回。
- 当前机器的 `D:\Temp` 无法被 pytest 扫描，运行全量测试时使用项目内 `--basetemp .pytest-tmp`，避免把权限错误误报为产品失败。
- 全量后端测试：**1742 passed / 6 skipped / 60 subtests passed**；仅有既有 Starlette 弃用提示和 pytest 缓存目录提示。

## 2026-09-28 自主开发试做区隔离与复核入口

- 自主工作流试做区的命令优先使用 Docker/Podman 和固定摘要镜像；Windows 没有容器时仍可用 Job Object 兼容路径继续工作，但界面明确标出这不是文件/网络隔离，独立终审标为 `unverified`，不能应用改动。
- 容器命令保持无网络，`network=true` 直接拒绝；公网研究仍使用已有审计的联网搜索。命令在临时副本运行，改动不会自动导入试做区；文件编辑仍走受控工具。
- 任务卡显示实际执行边界，已结束任务可重新测试与复核或丢弃试做区。应用试做区现在同时要求模型结果和独立项目复核通过。丢弃只删除经路径核验的本轮试做区，不碰原项目。
- 当前 LangGraph 已提供任务 DAG、并行子代理、人工中断与持久检查点；未引入第二套 Deep Agent 框架。真实 Docker/Podman 端到端联验仍待具备运行时和镜像的机器执行。

## 2026-09-28 Bing 单一来源降权

- `builtin_auto` 在 DDG 被风控、百度无结果时会自然落到 Bing；旅行/美食/攻略问题现会并行扩展知乎、小红书、B 站和贴吧等社区来源，最多 4 个补充站点。
- 中文推荐、口碑、旅行类结果中对 Bing 通用摘要做轻微降权，对社区原站做轻微加权；技术文档和代码查询排序规则不变。Bing 仍保留为候选和兜底，不再默认成为这类问题的主答案来源。
- 新增旅行来源扇出回归测试；未在当前受限网络环境证明各外站实时可达，真实网络下仍会按超时和失败结果自动剔除不可用来源。

## 2026-09-28 交通追问强制路由

- 追问“具体多少钱/目前有什么票”会结合当前会话的用户历史识别交通意图；缺少出发地、目的地或具体日期时在模型调用前澄清，不再把“明天”或搜索摘要自动当成日期/票价。
- 已对交通问题增加工具路由门：模型误调用 `web_search`、`web_search_batch` 或 `web_research` 时会被改道到 `web_transport`，避免把北京旅游攻略等 Bing 跑题结果当作票务证据。
- 新增追问澄清回归测试；定向天气/交通/联网搜索测试通过。服务需要重启后加载本轮后端改动。

## 2026-09-28 交通错误结果收尾

- 修复强制收尾仍把“相关性不足”的 Bing 候选原样展示给用户的问题。被相关性门丢弃的网页摘要现在只显示“未获得可核对结果”，不再泄漏北京旅游攻略等跑题页面。
- 交通最终答案增加票价/余票证据门：没有 `verified_fare=true` 的官方实时票证时，模型输出的金额、余票、车次和航班价格会被替换为明确的未确认说明。
- 新增错误结果收尾回归测试；Agent、天气、联网搜索相关测试 **75 项通过**。

## 2026-09-28 旅行交通查询约束

- 新增 `web_transport` 专用交通路由。高铁/飞机票价或余票问题必须提供出发地、目的地和未来的具体出发日期；缺少日期时在 Agent 调用模型前直接澄清，不再让模型自行假设日期。
- `web_transport` 只检索带日期的交通候选，并明确返回 `verified_fare=false`。搜索摘要、历史记忆或常见价格区间不得被当作当前票价；没有官方实时票证时禁止编造或估算，需引导用户到 12306/航司官方购票页核对。
- 交通工具加入联网能力登记、系统提示、失败标记和重复重试护栏；联网查询会按去掉日期/站点/套话后的检索意图计数，同一策略连续失败三次后要求换来源或收尾。定向天气/搜索/Agent 回归及新增交通测试通过；未做真实购票接口登录联验，公开搜索无法替代官方实时余票。

## 2026-09-28 截图反馈泄漏与对话长度修复

- Web 试玩反馈不再把“先提方案、等待审核”等内部指引拼进用户消息；通过独立 `ui_context` 传入请求级系统上下文，模型不得把内部指引回显为用户原话。旧会话加载继续清理历史遗留的 `【系统提示】` 前缀。
- 放宽工具观察、轨迹和历史回答的字符上限，历史回放不再按 700 字硬截断；实际裁剪交给模型 token 预算，避免长答案中途丢失。`read_file` 默认观察提高到 12000 字，并兼容 `path="..."` 参数。
- 增加连续重复句检测：只有模型连续复述同一句自然语言才触发一次纠偏；正常长方案不会因为长度本身被截断。重复工具参数的安全护栏仍保留。
- 验证：Agent/流式测试 56 项通过，前端 typecheck 通过；需重启本地服务加载本轮后端修改。

## 2026-09-28 开发舱审核与反馈可见性修复

- 开发舱主区域直接显示当前工作流卡片和方案全文，旧任务可从「继续旧任务」恢复；计划、验收条件、任务轨迹也跟随工作流显示。预览适配器设置折叠，减少方案区干扰。
- 将 `review={}` 和未执行任务从复核失败、质量评分与验收报告中排除；实际工具调用单独展示，0 次时明确说明尚未调用。项目能力画像中的工具改标为「可用工具」。执行后可读取验收报告。
- 「与 AI 沟通下一步」聚焦开发舱独立对话并在草稿为空时预填与任务阶段相关的问题，不自动发送，也不覆盖已有草稿。
- 方案选择 API 改为只记录选择；前端随后生成计划。项目执行仍须明确批准计划。已加路由回归测试，防止再次把选择方案当作自动执行授权。
- 验证：前端 typecheck/build、工作流测试 54 项、路由审批门测试 1 项通过；实页核对 5 个方案可见、0 工具与未生成报告状态正确、聊天预填可用。未选择用户真实方案或批准执行。桌面服务已重启加载后端变更。

## 2026-09-28 开发舱工作室与历史管理

- 修正开发舱与普通工作台共用对话：两个常驻 ChatDock 实例，普通工作台沿用原标签页会话，开发舱按当前项目持久化独立 `dev-` 会话 ID。聊天请求和上下文用量明确传入对应会话 ID；草稿、失败续聊、历史列表、会话切换/新建/删除按实例隔离。开发舱历史只列 `dev-`，普通工作台过滤它们。两处仍共享项目工作流与模型配置，这些本来是项目/全局状态。
- 预览反馈/开发舱预览反馈与聊天聚焦事件增加目标作用域；开发舱事件只交给开发舱聊天，其他工作台事件仍交普通聊天。开发舱标题改“开发舱对话”，隐藏其重复工作流历史和普通聊天的孤儿工作流提示。普通工作台原收起/展开偏好键保持兼容。
- 实页验证两个 dock 同时存在、开发舱草稿不会进入普通工作台，切页和刷新后分别保留；开发舱初始不显示普通历史。构建/typecheck 通过，页面错误日志为空。未发送真实模型消息，因此后端双会话请求仍待真实消息联验。

- 修正旧任务使“开始自主开发”永久禁用的问题：有目标即可点击“开始新目标…”，页面确认框列出非终态旧任务；确认后重新读取列表，逐条暂停，再启动新目标。发现新增旧任务会重新展示确认，暂停失败不会启动，跨项目/页面 generation 变化停止后续操作。取消不改变旧任务。
- 右侧开发/模型/审批控件限制占用高度并可滚动，聊天标题固定不收缩、正文独立滚动；移除整行旧任务横条与聊天冗长提示，讨论入口改为次要链接。实页验证有目标时启动按钮启用、确认框列出旧任务与新目标、取消保留记录；展开模型栏时标题底部与正文顶部相接，无重叠。类型检查与构建通过；未确认启动真实模型测试任务。

- 补充引擎 Web 试玩“截图并反馈给 AI”：复用同源全画面捕获（含 HUD），先展示静态截图，用户填写意见后经已有 `docmind:send-chat` 发送图片和文字到 ChatDock；提示先提方案/验收、修改前审核。支持重新截取、显示处理/阻塞/失败状态与项目切换清理；跨域或捕获失败不会悄悄改成文字发送，原生窗口仍需上传截图。
- 开发目标注明是持续开发任务，与聊天/画面反馈区分；增加把目标预填聊天的入口（不自动发送）；旧任务阻塞时明确说明，Ctrl+Enter 同样遵守阻塞条件。
- 补充验证：实际导出 Godot Web 试玩成功（3.5 秒），截图反馈取得含角色/地面/血条的真实图像，输入意见后发送按钮启用；目标预填聊天正确。未发送测试消息到真实模型，未验证真实模型修改闭环。

- 开发舱不再默认选中最近的旧任务；通过“任务历史”主动打开，终态记录可逐条删除或清空当前列表，非终态可先暂停。删除需要确认，只删任务记录，不回滚项目文件，也不删除聊天和长期记忆。本轮没有删除用户历史。
- 页面调整为中央项目输出/引擎画面与右侧 AI 协作；任务报表、时间线、回滚与验收等移入“任务详情”。预览保留尺寸调整、截图、框选与反馈；没有真实预览时明确显示空状态。
- App 保留唯一 ChatDock，用 Teleport 在常规区域与工作室间移动，避免切页卸载中断请求或使视觉反馈失去接收者。SceneRuntimePanel 复用同实例，增加开发舱宿主与关闭事件。
- 类型检查与生产构建通过。1280×720 实页验证聊天输入可见、历史列表与删除确认出现、引擎面板嵌入/关闭正常，页面错误日志为空；没有运行真实模型或启动引擎。确认弹窗导致浏览器自动化阻塞，未确认删除，另一个验证标签页正常；实际删除端到端尚未验证。
- 工作区包含其他平台未提交/已暂存修改，本轮保留这些改动，没有混合提交。界面截图在 `artifacts/autonomous-studio.png`。

## 2026-09-28 长期记忆与中断恢复

- 新增 `agent_memory.py`，无需向量服务/额外模型调用的 SQLite 记忆库，按项目与应用隔离；保存 fact/preference/workflow/episode、来源、证据、更新时间与重复次数，每个作用域最多 500 条记忆与 100 个任务现场。记录内容有界，凭据与图片 data URL 脱敏；数据库加入 gitignore。
- Agent 开始执行及每次 action/observation 时保存最近 6 条现场；异常保存具体脱敏错误，停止/断连保存中断状态。硬退出后恢复已写入的 checkpoint，未记录原因时明确未知，不能推测。超时、步数耗尽、截断、未验证结果不标记 completed。已输出 final 时提前落盘，防止客户端随后断开丢失结果。
- 会话保存/加载保留 checkpoint_id 与 failure_reason，避免重启重复恢复；会话详情读取可恢复硬退出现场，删除会话同时清除其 checkpoint。长期摘要与原始会话是独立数据，删除记忆不删除原始会话/日志。
- 每轮按当前问题召回历史摘要与明确用户偏好，标记为参考资料；用户最新指令及实际验证优先。仅自动提取明确的语言/回答风格请求，不推测所在地或人口属性；Agent 可用 `memory_save` 存有证据的偏好、事实和流程，用 `memory_search`/`memory_forget` 检索/纠正。模型报告不当作已验证事实。
- 新增 GET `/api/agent/memory` 与 DELETE `/api/agent/memory/{id}`，当前没有新增前端记忆管理面板。常用流程复用既有 `dev_skill_create` 起草 SKILL.md，提示 Agent 写触发条件/步骤/验证/失败恢复，待用户确认后才 `dev_skill_approve` 激活；本轮不是无审核自动执行技能，也没有额外后台模型提炼任务。
- 定向记忆、会话恢复、Agent、天气、原生工具、并行、技能及权限回归 **156 passed**，只有既有 Starlette 弃用提示。覆盖错误脱敏、GeneratorExit、checkpoint 模拟硬退出、跨会话/项目隔离、显式偏好引用、并发写入、记忆 API 删除与技能草稿隔离。尚未做真实模型中断/硬杀进程联验；旧日志无法补回过去未记录的现场，服务需重启加载代码。

## 2026-09-28 搜索并发、数量与相关性修复

- 自动搜索在同一批次请求 DDG/百度/Bing 与相关社区/代码站点，使用共享 16 线程池；默认 5 秒汇总已完成来源，不等待最慢请求，也不再全失败后重复串行请求。运行中的 HTTP 请求仍受各自超时控制，密集查询可能占满线程；独立脚本退出会等待这些线程，常驻服务中的工具返回不等待它们。
- HTML 后端最多取 10 条，自动/批量聚合默认最多 30 条；API 搜索请求增加到 20–30 条，Exa 搜索不再同时下载正文。可用 `DOCMIND_WEB_SEARCH_RESULTS`（5–50）与 `DOCMIND_WEB_SEARCH_WAIT_S`（0.05–15 秒）调整自动模式；显式单引擎/API 仍遵守自己的网络超时。
- 按关键词匹配优先排序并拒绝仅命中泛词的多关键词结果，普通教程不再强制近一年；这是确定性关键词过滤，不能保证语义相关性。修复 DDG 属性顺序/HTML 实体和 Bing 实体转义跳转链接，更新搜索缓存版本。
- 单次及批量搜索观察默认保留最多 12000 字符，减少候选传给模型时被提前截断；更新模型使用说明，区分搜索候选与已核对正文。
- 定向联网、天气恢复、Agent、原生工具、并行与图像回归 **166 passed**；只有既有 Starlette 弃用提示。慢来源/30 候选测试使用模拟数据，不是联网吞吐证明。
- 关闭缓存真实查询 `Godot scene tree official docs` 和限定 `docs.godotengine.org` 均约 **5.03 秒**返回相关性不足，成功拒绝此前跑题的首页；当前免费 HTML 来源未找到有效候选。Bing 国内/国际入口仍返回泛化结果，因此没有证明瞬间返回 20–30 条有效来源。后续需真实比较可用的搜索 API/自建搜索服务与中文查询质量；不能编造候选来凑数量。
- 运行中的旧服务需要重启以加载修改；本轮未替用户停止服务。

## 2026-09-28 天气地点/日期约束与失败续聊

- 天气问题未提供地点时直接澄清，不让模型从搜索结果猜城市；每轮注入本机当前日期/时区，今日查询补充具体日期。
- 新增 `web_weather`，使用 Open-Meteo 地理编码和结构化预报，要求明确城市、同名地点确认、今日日期一致；这是模型预报，不是实测观测。接口失败时明确降级，不编造天气。
- Ollama 对图片明确返回 HTTP 400 时，只尝试一次文本请求并标注未读到图片；其他错误不会盲目重试。
- 失败回合保存有界工具现场，重启恢复后输入“继续”可回放来源/观察；继续及补充天气城市时沿用原任务的工具路由。失败 SSE 与前端状态改为 error，避免误标已完成。
- 定向后端回归 161 passed；前端 typecheck/build 通过。真实模型/天气服务联验尚未完成，运行中的旧服务需重启加载代码。

## 2026-09-28 生成领域适配器 fixture 与预览证据

- `3525b5c feat: add adapter fixture replay`：新增 `dev_preview_adapter_test`，用固定 JSON fixture 重放已激活的 Python/Node 适配器；不调用真实软件、MCP 或网络，便于先验证 EasyEDA/Godot/CAD 数据转换。
- `a1939b3 feat: record generated adapter preview evidence`：生成适配器刷新结果现在转换为标准 `ToolResult` 和预览 artifact，带适配器、来源工具、模块路径元数据，并进入 Agent 事件和工作流预览证据链；原始结构化结果保留在 `data`。
- 定向适配器/桌面回归：**42 passed**（仅现有 Starlette 弃用提示）。两个提交已推送到 `origin/main`。
- 仍待具备对应软件或真实 MCP 后进行 EasyEDA/Godot/CAD 联验；通用链路已具备 fixture 测试、隔离执行、回滚、桌面捕获和审批动作入口。

## 2026-09-27 项目级能力画像

- 新增 `agent_runtime/project_profile.py`，每个项目在 `.docmind/project-profile.json` 保存脱敏的能力摘要：所用工具、MCP 连接器、运行命令、验收方法/脚本、预览适配器、来源和领域类型。
- 工作流保存时自动合并当前任务、验收契约、预览 artifact 和已配置 MCP；工作流进程重启时从项目画像恢复。画像不会写入 MCP command、args、env、headers、token 或其他凭据。
- 新增 `GET /api/agent/project-profile`，开发舱项目切换时重新读取；右侧“项目能力画像”卡片展示连接器、工具、运行命令、验收方式和预览适配器。
- 新增 `tests/test_project_profile.py`，覆盖首次创建、工作流重启恢复、项目隔离和敏感字段脱敏。
- 验证：全量后端测试 **1642 passed / 6 skipped / 60 subtests passed**；前端 typecheck 与 production build 通过，仅保留既有非 module 脚本和大 chunk 警告。

## 2026-09-27 工作流级模型自我复盘

- 新增 `agent_runtime/workflow_reflection.py`，从任务结果、文件/预览 artifact、复核检查、验收契约、权限租约和子代理 reflection 生成有界、脱敏的 `self_review`。
- 工作流完成、失败或暂停时自动持久化复盘；旧终态工作流首次读取时自动迁移。复盘分为“做了什么、验证了什么、仍不确定、建议下一步”，带可信度和证据来源，不额外调用模型，不把原始工具输出或凭据暴露给前端。
- `AutonomousCockpit.vue` 右侧新增“模型自我复盘”卡片，用户可以直接看到未确定项和下一步，而不需要翻完整时间线。
- 新增 `tests/test_workflow_self_review.py`，覆盖终态重启恢复、失败任务、不确定项、用户验收和敏感字段脱敏；补修 `WorkflowCard.vue` 缺失的 `cardOpen` 状态，前端 typecheck/build 恢复通过。
- 验证：全量后端测试 **1647 passed / 6 skipped / 60 subtests passed**；前端 typecheck 与 production build 通过，仅保留既有非 module 脚本和大 chunk 警告。

## 2026-09-27 项目级工具环境锁定

- `ToolInstallManager` 在项目 `.docmind/tool_envs` 内安装后写入 `.docmind/tool-lock.json`，记录 PyPI/npm 来源、精确 spec、隔离目录、启动方式、安装时间和内容 SHA-256/文件统计。
- 安装审计记录同步包含来源和启动方式；哈希计算有文件数和 50MB 上限，避免大型环境阻塞工作流。
- `tests/test_tool_install.py` 已覆盖 lock manifest、来源、启动方式和 fingerprint；定向工具测试 **6 passed**。

## 2026-09-27 并行代理文件冲突保护

- `tools.py` 为每个绝对文件路径增加进程内写锁，`apply_edit` 在最终落盘前重新读取并比对基线；并行代理已修改文件时直接返回“并行修改冲突”，不会覆盖新内容。
- `create_file` 和人工确认后的落盘也在同一文件锁下再次检查目标，避免两个代理同时创建或确认过期修改。
- 新增 `tests/test_file_conflict.py` 覆盖落盘前文件变化的 fail-closed 行为；定向工具/并行测试 **11 passed**。

## 2026-09-27 任务时间线与安全续跑

- `WorkflowState` 新增持久化 `timeline`，最多保留 500 条事件；原有 `events` 继续作为 100 条 SSE 重放窗口，旧状态读取时自动迁移。
- 开发舱改为展示可展开的任务时间线，记录计划、审批、工具/MCP、快照、视觉证据、验收和暂停/恢复事件；失败或阻塞任务可从该任务继续，复用已有幂等重试和重试上限。
- 新增 `tests/test_workflow_timeline.py` 验证长时间线、SSE 窗口和进程重启持久化；完整回归：**1638 passed / 6 skipped / 60 subtests passed**。前端构建通过。

## 2026-09-27 工作流质量评估接入开发舱

- 开发舱读取既有 `/api/agent/workflow/{id}/evaluation` 评估接口，在工作流完成、失败或暂停后展示评分、任务/步骤/重规划次数、失败/阻塞/不确定副作用数量及逐项检查结果。
- 评估请求属于增强信息；接口暂时不可用时不影响工作流状态、恢复方案和预览展示。
- 前端 typecheck 与 `npm run build` 通过，仅保留既有非 module 脚本和大 chunk 警告。

## 2026-09-27 失败恢复方案接入自主开发舱

- 工作流失败、子代理重试失败或进程恢复失败时，会从持久化结果生成脱敏的 `recovery` 计划，只暴露任务 ID、数量和错误类别，不暴露工具参数、文件内容或模型输出。
- 计划最多提供三类可审核动作：先确认不确定副作用、按任务重试失败/阻塞任务、恢复执行前项目快照；没有安全动作时引导查看失败证据并重新规划。
- `AutonomousCockpit.vue` 右侧新增“失败后的下一步”卡片，重试按顺序执行并保留确认框，快照恢复复用既有回滚接口，查看证据复用对话审核卡片。
- 新增 `tests/test_recovery_plan.py` 覆盖失败任务、不确定副作用、快照和无证据场景；完整回归：**1637 passed / 6 skipped / 60 subtests passed**。前端 typecheck 与 `npm run build` 通过，仅保留既有非 module 脚本和大 chunk 警告。

## 2026-09-27 工具权限租约与自主开发舱能力范围

- 工作流新增持久化 `capability_lease`：执行阶段默认临时授予完整的 `read_local`、`read_external`、`write_local`、`write_external`、`exec`、`network`、`admin` 能力，默认 30 分钟到期（可用 `DOCMIND_CAPABILITY_LEASE_S` 调整，范围 5 分钟至 2 小时）。
- `Agent` 在串行和并行工具执行边界检查租约；过期或不包含工具能力时只回填权限租约观察，不执行工具。子代理继承父 Agent 租约，现有用户审批、白名单、幂等和副作用保护仍是最终边界。
- 工作流终态（完成、失败、中断）自动标记租约为 `released`；进程重启后的工作流回调按工作流 ID 重新读取租约。开发舱右侧显示能力、状态和到期时间。
- 新增 `tests/test_capability_lease.py`；完整回归：**1632 passed / 6 skipped / 60 subtests passed**。前端 `npm run build` 通过，仅保留既有非 module 脚本和大 chunk 警告。

## 2026-09-26 非流式取消与预览重启持久化补充

- `llm.py` 的云端 OpenAI 兼容调用在收到 `cancel_event` 时，会临时使用流式响应并在客户端聚合为原有字符串；取消会关闭 SDK 流、终止当前回合，不再受非流式 `resp` 阻塞限制。未提供取消事件的普通非流式调用保持原路径。
- Ollama 工作流子代理的非流式调用同样在有取消事件时使用可关闭的 NDJSON 流聚合；GPU lease 覆盖整个生成和收尾，取消后由流适配器释放。
- 新增 `tests/test_llm_resilience.py` 的云端非流式正常返回与取消回归；`tests/test_game_workflow.py` 验证包含截图 artifact 的工作流保存后，经新 `GameWorkflowManager` 实例恢复仍可读取预览证据。定向测试：**84 passed**；`py_compile` 通过。
- 之前本节中“非流式云端请求仍依赖 SDK timeout”的描述属于更新前状态，以本补充为准。
## 2026-09-26 生产视觉验收与云端取消补充

- 正式工作流已接入 `agent_runtime/visual_acceptance.py` 与 `tools.preview_project`：网页项目的 tester 子代理可以启动临时 Edge、捕获真实截图、把截图送入视觉通道，并通过 `artifacts` 写入工作流 `preview` 证据。开发舱现有通用预览会直接显示该截图。
- `self_verify` 支持显式 `scope: visual`，会调用同一浏览器适配器并返回截图路径与检查结果；跨域、原生窗口、视频和非网页项目仍需领域专用适配器。
- `StreamChat` 与 `_OllamaStream` 都监听 `cancel_event`，停止/断连时关闭底层响应；Ollama 和 OpenAI 兼容云端流均覆盖。非流式云端请求仍依赖 SDK timeout，不能宣称任意时刻强制中断。
- 定向回归：87 passed / 4 subtests；正式视觉适配器已在沙箱外用隔离网页实际启动 Edge 并捕获截图。真实 `qwen3.6:35b-a3b-agent256k` 生产链路报告为 `.docmind/docmind-production-visual-9h2yej50/evidence/report.json`：文件确有修改、模型调用了生产 `preview_project`、截图 artifact 已登记、`verified=true`、`passed=true`。前端 typecheck 通过。

> 生成时间：2026-09-25 16:55 GMT+8（接手状态与验证结果于 2026-09-25 更新）
> 冻结基线 commit：**`80b8a90`**（= origin/main，验证基线）
> 当前 HEAD：**`ecea19a`**（N3 已合入，未推送）
> 适用范围：DocMind 后端 `mcp_*.py` + 前端 `SettingsView.vue` / `api.ts` 的 MCP 自动连接（auto-connect）链路
> 形态：单人桌面 GUI（Python FastAPI + Vue3 + PyInstaller/pywebview），非 SaaS

---

## 最新接手点 · 2026-09-26 真实模型视觉修改验收

### 继续推进 · 通用网页截图与自动复验已接入开发舱

- 新增 `frontend/src/workbench/previewCapture.ts`，同源 iframe 现在可以截取普通 DOM、文字、图片和 canvas 的完整视口，不再要求页面必须有 canvas。截图前检查跨域、未加载图片、嵌套页面、视频和污染 canvas；无法完整证明时返回明确原因，不向模型发送假截图。
- 视觉反馈发送前仍保存修改前截图；AI 回复结束后自动刷新同一预览 iframe，等待加载完成，再保存修改后截图。自动截图失败会把反馈留在「等待检查效果」，显示失败原因，用户可以手动重试或记录；自动截图不会替用户点击「效果满意」。
- 自动复验绑定发起反馈的工作流和项目，切换项目或画面后不会把另一项目的截图写入旧反馈；原始截图和复验截图分开保存。
- `html2canvas` 已加入前端依赖，生产构建通过。`tests/ui/cockpit_feedback_smoke.py` 现有隔离浏览器检查扩展到 **28 项通过**：普通网页+画布同图、普通 DOM 无 canvas、滚动视口、刷新后的新画面、截图失败、跨域/视频拒绝和错误状态均覆盖；项目绑定也有检查。
- 慢响应体验已补强：聊天回合记录最后事件时间，连续15秒没有事件时显示模型/工具可能仍在处理的提示；服务端进入 Agent 前发送“已进入模型处理”通知；用户主动停止前保存可继续现场，恢复入口复用既有“继续上次任务”。这只改善反馈和恢复，不改变模型/GPU参数。
- `/api/chat` 已把同步 Agent 消费放入后台队列，SSE 每15秒发送一次存活提示，即使 Ollama 在首个 token 前长时间预填充，连接也不会看起来像断开。后台线程只传递已产生的 Agent 事件；客户端断开会设置停止标记，已产生的工具副作用仍按现有审计和恢复规则处理。
- 相关后端回归：`tests/test_visual_feedback.py tests/test_preview_adapters.py tests/test_runtime_contracts.py tests/test_workflow_chat_stream.py`：**48 passed，4 subtests passed**；另有聊天 SSE 心跳专测通过。前端 `npm run build` 通过，仅保留原有非 module 脚本和大 chunk 提示。

范围限制：截图依赖同源 DOM。跨域页面、嵌套页面、视频和被污染的 canvas 仍需要领域专用适配器；系统会回退为文字反馈并明确告诉模型没有收到画面。`html2canvas` 只负责可见网页效果，不验证源码、功能或模型声明；最终验收仍由用户完成。

本轮新增真实模型验收脚本 `tests/manual/cockpit_live_validation.py` 和普通 DOM 网页示例 `tests/fixtures/cockpit-live/index.html`。使用本机 Ollama 已加载的 `qwen3.6:35b-a3b-agent256k`，运行真实 `Agent(tool_mode="native")` 和生产 `read_file` / `apply_edit`，在独立示例目录实际修改文件。模型/GPU 全局配置未改动，不访问用户真实项目。

- 写后自动校验现在优先使用当前 Agent 实例注册的 `self_verify`；未注册时仍使用原有校验器。项目可提供真正适用的校验，失败仍阻止模型宣称完成。
- 验收适配器通过隔离 Edge 返回真实截图，并检查按钮位置、尺寸、字号、文字对比度和两次真实点击行为。随机六位编号只出现在截图里，用于验证模型确实接收视觉输入。
- 增加源码检查，拦截已发现的 `Observation: [apply_edit result]` 工具结果占位文字、`<reset_dir_prompt>` 对话标记，并检查状态文字样式，避免浏览器静默忽略非法 CSS 或陌生 HTML 标签后产生假通过。这个约束只属于示例验收，生产文件工具没有全局禁止或删除用户内容。
- `tests/test_runtime_contracts.py tests/test_self_verify.py tests/test_tool_vision_channel.py tests/test_vision_workflow_e2e.py`：**45 passed / 4 subtests passed**；生产修改与手动脚本编译通过。
- 复现：`.venv\Scripts\python.exe tests/manual/cockpit_live_validation.py --model qwen3.6:35b-a3b-agent256k`。每次保存在 `.docmind/cockpit-validation/<时间与随机后缀>/`，含示例源码、前后截图、工具事件和 `evidence/report.json`；隔离状态不会覆盖用户设置。当前环境浏览器需沙箱外运行。
- 支持 `--resume-report <旧报告>` 从失败的第二轮继续：只允许独立验收目录里的文件，复制失败源码到新的运行目录，保留旧证据和已通过的第一轮。验收器返回具体失败值，例如实际对比度及4.5阈值。示例 Agent 允许24次工具调用，以容纳修复与预览；此设置不改变产品的连续执行策略。

真实现场结果（以报告链为准，不是一次无中断运行）：

- `20260926-172226-8231/evidence/report.json` 第一轮 **44.13秒通过**：按钮由越界150×24、字号11、对比度1.13，改为居中200×44、字号16、蓝底白字、对比度5.17。模型正确读出仅存在于截图中的编号 **139607**。第二轮未达标且后续响应超过十分钟，已停止；`interruption.json` 单独记录中断，原报告保留为未整体通过。
- `20260926-173701-cf86/evidence/report.json` 第一次续修改善对比度，但残留对话标记、未实际调用预览，**不通过**。这轮暴露了源码质量与默认工具预算不足的问题。
- `20260926-173951-a337/evidence/report.json` 最终续修 **100.23秒通过 / passed=true**，链接前两份报告。模型自己删除残留标记，真实调用 `preview_project({})` 取得 `preview-3.png`。最终为深绿色按钮「再次运行」、居中200×44、字号16、对比度 **8.29**；两次浏览器点击显示「运行次数：1」「运行次数：2」，源码、样式和所有浏览器检查通过。验收脚本现在按实际成功执行的预览计数，缺参拦截事件不算已预览。
- 最终源码：`.docmind/cockpit-validation/20260926-173951-a337/project/index.html`；真实前后拼图：同运行 `evidence/comparison.png`。所有失败证据均保留，无人工替模型修复示例源码。
- 较早的 `20260926-171555-586c` 报告只覆盖旧版功能检查，后续发现样式污染；它不是完整源码质量通过的证据。

范围（本节记录的是接入前的历史状态）：这是**真实 Agent + 文件工具 + 浏览器验收适配器**的现场验证。当时适配器尚未注册到正式开发舱，普通 DOM 截图也尚未接入正式预览服务。后续第 14 节已完成通用网页截图、自动复验和生产 `preview_project` 接入；当前仍需在实际项目上联验正式 UI、真实 MCP、联网下载和高风险审批，非网页领域仍需专用适配器。上一节所述“真实模型修改待验”以本节后续更新为准；其他产品限制仍有效。

## 上轮接手点 · 2026-09-26 自主开发舱视觉反馈闭环

当前重点已从 MCP 搜索扩展为通用自主开发舱。用户要求能力覆盖任意领域，EDA、游戏只是例子；只服务当前项目，可连续执行，用户审核计划、需要确认的操作与最终效果。既有 MCP 连接与能力审批仍必须由用户完成。用户已暂停 Ollama/GPU 性能优化，后续不要重新改动这部分配置。

本轮实现（未提交）：

- 预览可拖动调整宽高、刷新、全屏、框选区域并打开反馈浮窗；修复 `/play/...` 等同源相对预览地址。
- 工作流保存最多 20 条视觉反馈，状态区分待发送、AI 处理中、等待检查效果、失败、效果已确认和需要继续修改。
- 修改前/复验 PNG 截图保存在 `.docmind/workflows/visual_feedback/{workflow_id}/{feedback_id}/`，JSON 仅存尺寸、大小、时间等信息。原始截图不可覆盖，复验截图允许更新；实际图片内容和路径经过校验，读写接口检查当前请求项目。
- 对话组件明确确认接收反馈；忙碌或等待审批时保留反馈并说明原因，用户可重新发送。重试读取已保存的原始截图，提醒模型先核对当前状态以免重复副作用。画面反馈不清空聊天草稿或用户附件。
- 反馈状态按顺序保存，避免慢响应覆盖完成状态；保存请求绑定发起反馈的项目。查看/评价历史反馈不会重新启动历史工作流的项目互斥。
- 用户刷新预览后选择「记录当前效果」，并排查看修改前/复验画面，再选择「效果满意」或「继续修改」。AI 回复结束只表示等待用户检查；反馈评价不替代整个工作流的最终验收。

验证证据：

- `.venv\Scripts\python.exe -m pytest tests/test_visual_feedback.py tests/test_game_workflow.py tests/test_preview_adapters.py tests/test_cockpit_policy.py -q`：**77 passed**，仅现有 Starlette 测试客户端弃用提示。
- 前端 `npm run build`（含类型检查）：通过；仅既有非 module session 脚本和大 chunk 提示。
- `tests/ui/cockpit_feedback_smoke.py`：隔离 Edge 中实际挂载开发舱和对话组件，**15 项交互检查通过**，包括尺寸调整、框选、接收图片、草稿保留、忙碌反馈、重试、乱序响应、截图展示、组件重新打开和页面无运行异常。使用受控模型/存储服务，不能等同于真实模型修改项目已验收。检查截图位于 `.docmind/cockpit-feedback-smoke.png`（忽略文件）。
- UI 复现：先在 `frontend` 启动 `npm run dev -- --host 127.0.0.1 --port 5179 --strictPort`，再于仓库根运行 `.venv\Scripts\python.exe tests/ui/cockpit_feedback_smoke.py`。测试使用临时浏览器配置和临时测试页，不访问真实项目服务；当前受限环境中浏览器须在沙箱外运行。

实际限制和下一步：

1. 自动截图支持同源 iframe 中可读取的普通 DOM、文字、图片和 canvas；跨域画面、嵌套页面、视频、原生软件窗口以及被污染的 canvas 不能由通用链路直接截图。无法完整截图时明确显示文字反馈；WebGL 缓冲内容是否可读取由项目渲染方式决定。
2. 真实模型结合截图修改通用示例项目的生产链路已有现场通过证据；仍需在正式桌面 UI 和实际用户项目上复验，尤其是不支持原生视觉的模型所使用的视觉辅助链路。受控 UI 测试不能替代这类验收。
3. 接下来完善通用预览服务的截图适配能力、失败/中断后的恢复与不确定副作用处理；沿用设计文档 Phase 4，保留现有审批门。
4. 大量既有 MCP/工作流改动仍未提交；不要 reset、clean 或擅自提交/推送。保留后来出现的 `.docmind_model_context.json`。

原 MCP 交接与历史测试记录保留如下，其结果属于对应历史状态。

## 0. 一句话背景

「设置 → MCP → 搜索连接方式」链路：让大模型调用工具自己查文档、自己连，遇需注册的弹窗说明「只需帮忙注册」。N3 已在 `ecea19a` 完成；当前工作树中的可选密钥语义、领域目录候选、模块拆分、Agent 自然语言调度和用户审批门已通过测试，尚未提交。2026-09-26 已用真实 EasyEDA MCP 完成 Registry 发现、握手、19 项工具发现、临时项目能力审批及只读状态调用；编辑器扩展未安装，实际图纸操作待验。后端搜索接口及 Agent 的 `dev_mcp_search` 工具均已在空临时项目中真实查到 Registry 候选，搜索不写连接配置；受控模型测试已证明调度链路，真实模型自然语言会话仍待可用模型验收。MCP 连接器安装和能力路由的审批现在只能由设置页用户操作写入，Agent 的 `dev_approve` 对这两类操作会拒绝。验证基线、源码快照和真机结果记录在 `docs/mcp-autoconnect/verification-2026-09-25.md`。工作树还包含自主开发舱设计文档及一个本地模型上下文文件，均为既有未跟踪项。

---

## 1. 当前状态（接手必核）

```bash
git log --oneline -6
git status --short
```

- **HEAD = `ecea19a`**；`origin/main` 仍为 `80b8a90`。5 个历史 junk 文件已于 2026-09-25 删除（`.docmind_model_context.json` / `.docmind_state.json.bak-qa-cleanup` / `api.py.bak-pre-autoconnect` / `frontend/tsc-check.txt` / `mcp_client.py.bak-pre-autoconnect`）。当前新增的 `.docmind_model_context.json` 是后来出现的未跟踪文件，保留原样。
- **本轮已交付 6 笔提交（全在 main）**：

| commit | 内容 |
|---|---|
| `df49458` | 适配器误路由修复：`provenance`（来源仓库）不再参与 provider 匹配；未收录 provider 的 `register_url` 置空（堵误跳 GitHub PAT 页） |
| `2d098a9` | Registry 检索瞬时超时重试（attempts=2）+ 网络错误中文友好化（不再裸透 `TimeoutError`） |
| `d2fce2e` | 收录 smithery 适配器（`register_url=https://smithery.ai/account/api-keys`）+ 未收录分支文案增强 + 回归测试改写 |
| `29f03b9` | smithery L2 文案点明「需先登录」（未登录点深链回落首页） |
| `b2f04a8` | 候选卡片三项修复：标题显服务名 / 信任徽章分 official·community·unknown / 试连 404 中文友好化 |
| `80b8a90` | `McpProvenance` 类型放宽（`server_name`/`namespace`/`registry`/`curated` 可选），修 `vue-tsc` TS2339 |

- **历史测试基线**：`tests/test_mcp_autoconnect.py` **84 passed**；全量 `tests/` 基线 **232 passed / 4 skipped**（来自旧交接记录）。当前工作树全量测试 **1582 passed / 5 skipped / 60 subtests passed**（临时使用 `LLM_PROVIDER=mock`、`EMBEDDING_PROVIDER=local`，避免本机 Ollama 不可达）。

### 三条安全不变量（改任何 MCP 代码都不得破）
1. `mcp_client.py:405` 是 `subprocess.Popen([command, *args])`：写进 `.docmind_mcp.json` 的 `command`/`args` 会被**真实执行**。凡「让模型自动填连接参数」的设计 = 开放本机任意代码执行入口 -> R8 自动填参闸门只允许**外部副作用自动、本地副作用人工确认**。
2. **df49458 不变量**：未收录 provider 的 `register_url` **恒为空**，绝不回退 `provenance.url`（那是源码仓库，会跳错站）。
3. `trust` 字段仍供 R8 闸门使用；`trust_tier` 仅 UI 分档，勿混用。

---

## 2. 接手 backlog 状态

### ✅ N2 · 验证期仓库移动靶冻结 【已记录】
- `docs/mcp-autoconnect/verification-2026-09-25.md` 固定了对比基线 `80b8a90`、本地 HEAD `ecea19a`、未提交源码的 SHA-256、测试结果及 2026-09-26 真实 EasyEDA MCP 的现场探测结果。真实图纸读写与需账号注册流程仍待具备对应软件和账号的环境验收。

### ✅ N3 · `acNeedRegister` 死标志清理 【已完成 · 2026-09-25 · commit `ecea19a`】
- **落点**：`frontend/src/workbench/components/SettingsView.vue`
  - `:164` `const acNeedRegister = ref(false)`（声明）
  - `:596`、`:721` 两处 `acNeedRegister.value = false`（置位）
  - `:1039` `<button v-if="c.config.command_unresolved || acNeedRegister || acNeedsSecret(c)" ...>`（仅此引用，且恒 false）
- **根因**：L2 改造前的遗留标志，从未被置为 `true`。
- **动作**：删声明 + 两处置位行 + `:1039` 去掉 `|| acNeedRegister`。纯删除，无行为变化。
- **验证**：`vue-tsc --noEmit` 退出码 0；全仓 grep `acNeedRegister` 代码引用 = 0。接手 AI **跳过此项**。

### ✅ (b) · Registry 可选密钥语义 【已实现，验证完成】
- Registry stdio 环境变量与 remote headers 输出 `secret_specs: [{name, required}]`，候选视图传给前端 `secrets`；旧候选没有元数据时仍按 `@secret:` 视为必填，保持兼容。
- 注册表单按必填/可选显示；只有必填项阻止提交。只有可选密钥时不启动自动注册流程，用户可主动打开表单填写；全部留空直接关闭，不发送空提交。后端解析缺失的可选密钥为空串，HTTP 空请求头会被移除。
- **验证**：最新 MCP 相关定向测试 **136 passed**；接通 Agent 搜索工具后的全量测试（临时使用 mock/local provider，避免本机 Ollama 不可达造成两项无关早退）**1579 passed, 5 skipped**。问答首页静态工作台链接已补齐，桌面入口测试通过。前端 `npm run build`（含类型检查）通过，仍有既存非 module 脚本与大 chunk 提示。

### ✅ `headers_sent` 字段 【已核实，无需处理】
- 全仓 `rg` 搜索（排除依赖目录）无匹配；当前源码没有该响应字段或对应实现，故从 backlog 移除。

### ✅ 模块抽取 · `mcp_autoconnect.py` 【已完成】
- 主模块从本轮开始时的 1559 行降到 997 行。候选构造和诊断图迁至 `mcp_candidates.py`，服务适配器表与匹配规则迁至 `mcp_providers.py`，R1–R9 校验迁至 `mcp_validation.py`，Registry 接入边界迁至 `mcp_registry_bridge.py`。
- 浏览器会话与注册状态机仍留在主模块；原有导入入口兼容，相关测试及全量测试通过。

### ✅ Agent MCP 调度与用户审批门 【已完成 · 2026-09-26 · 尚未提交】
- `agent_runtime/context_router.py` 在 MCP 连接意图出现时开放 `developer` 工具组；`dev_mcp_search` 会继承会话联网开关并查询真实 MCP Registry。
- `tests/test_mcp_self_assembly_tools.py` 的受控模型测试证明自然语言请求会收到并调用 `dev_mcp_search`，且搜索不会写入 `.docmind_mcp.json`。这验证调度链路，不等同于真实模型已验收。
- `tools.dev_approve` 不再允许 Agent 写入 `mcp_server` / `mcp_capability` 审批；设置页保存、移除连接器及批准能力时记录 `workbench-user` 审批，绑定具体连接参数或 key，用户确认后 Agent 重试才会放行。
- 全量测试最新结果为 `1582 passed / 5 skipped / 60 subtests passed`；前端 `npm run build`（含 `vue-tsc`）通过，只有既有脚本类型和大 chunk 提示。

---

## 3. 接手环境坑（必读，避免重踩）

1. **真实代码位置**：`D:\WorkBuddy\rag-agent`。沙箱 worktree（`C:\Users\h'h'h\WorkBuddy\Worktrees\rag-agent\main-b8047d2e`）是镜像，**改真实仓库**，不要写 worktree。
2. **沙箱 Bash PATH shim 坏**：须手动 `export PATH=".../PortableGit/.../usr/bin:.../bin:$PATH"`；且 python/node 用**绝对全路径**（单引号用户名会破坏 Git Bash 引号，无 `ls`/`tail`/`cat`/`wc`）。
3. **沙箱 managed python 缺依赖**：缺 `openai`/`chromadb` -> 全量 `pytest tests/` 冒大量 collection ERROR（`ImportError: from openai import OpenAI`），**与本仓库改动无关**。自测须用项目 venv：`D:\WorkBuddy\rag-agent\.venv\Scripts\python.exe`（CI 同理）。
4. **提交/推送**：沙箱 MITM 代理破坏 `api.github.com` 鉴权 -> 提交须用户本机 PowerShell `git push origin main`。
5. **前端构建产物**：`web/workbench.html` 与 `web/assets` 被忽略；`web/index.html` 是已跟踪文件。本轮修改 `frontend/index.html` 后已运行 `npm run build`，因此 `web/index.html` 也更新。不要把被忽略的资源目录误加入 Git。

---

## 4. 建议接手节奏

1. 已完成本交接列出的代码与验证记录工作；本地代码改动尚未提交。
2. 真实第三方 EasyEDA MCP 已完成发现、试连、工具发现、临时项目能力审批及只读状态调用，详见验证记录。当前机器未装 EasyEDA Pro 扩展，实际设计图纸操作需在有编辑器的机器验收；需要账号的注册流程仍需真实账号验收。

---

## 5. 关键文件速查

| 文件 | 职责 |
|---|---|
| `mcp_autoconnect.py` | auto-connect 主模块（pipeline 编排、候选视图 `_candidate_view`、provider 适配器表 `PROVIDER_ADAPTERS`、误路由修复 `select_provider_adapter`、L2 桩 `browser_register`） |
| `mcp_registry.py` | 官方 MCP Registry 检索（`search_registry` 有界重试、`parse_registry_response`、`server_to_candidates`、`rank_candidates`、`RegistryCache`）；`variables[].isRequired` 在 `:180` |
| `mcp_server_index.py` | 离线精选索引（`curated_entry_to_config` 补 `server_name`） |
| `mcp_client.py` | `_http_post` / `_http_status_message`（:600 附近，404 提示已加「服务可能已下线」）/`probe_server` |
| `api.py` | `/api/mcp/autoconnect/*` 端点（`:3368` probe 端点捕获 404 回中文友好文案） |
| `frontend/src/workbench/api.ts` | `McpAutoConnectCandidate` / `McpProvenance` / `AcSource` 类型 |
| `frontend/src/workbench/components/SettingsView.vue` | 候选卡 UI：`acCardTitle`(标题取 server_name)、`acTrustTag`(徽章分档)、`acSecretProviders`(:373)、`regFields`(:644) |

> 更细的真机复现证据、匹配逻辑、commit diff 见 `.workbuddy/memory/2026-09-25.md`「接手 handoff」节。
# 2026-09-28 自适应多来源搜索与批量检索

- `tools.py` 的自动联网搜索会按查询语义并行补充 GitHub、B 站、知乎、百度贴吧、小红书、微博、CSDN、Stack Overflow 等相关站点，并跨来源去重排序；保留 DuckDuckGo、百度、Bing 的通用搜索结果作为广度来源。
- 新增 `web_search_batch`：多个查询并行执行，按 URL/标题去重，并可通过 `exclude` 排除上一轮已看过的结果，支持候选、评价、教程、做法等多轮搜索。
- `web_search` 与 `web_research` 的工具描述明确引导 Agent 根据观察动态改写查询、并行查证、排除重复结果后继续下一轮。
- 定向联网回归：**39 passed**；Agent/并行工具回归：**88 passed**；`py_compile` 通过。

# 2026-09-29 顶栏入口合并

- `frontend/src/workbench/App.vue` 将原“代码图”和“工具”两个顶栏下拉合并为统一的“更多”菜单，保留代码地图、关系图、Unity 图、流程图、任务与生成、GPU 监控、AI 运行台、AI 运行设置、运行游戏 Teleport 槽位和引擎连接入口。
- `frontend/src/workbench/style.css` 增加菜单分组标题样式，并更新入口说明；没有修改面板调用、工作流、审批或工具数据链路。
- 验证：前端 `npm run typecheck`、`npm run build`、仓库 `git diff --check` 均通过。构建仍只有既有的 `/static/session.js` 非 module 脚本和大 chunk 提示。

# 2026-09-29 前端工作流层次整理

## 2026-09-29 R14 进度同步 + 重复防护 + 代码审查（AI-H）

- **职责**：维护实时音视频任务表公告区（谁负责、做到哪里、卡在哪、下一步），防止多个 AI 重复完成同一任务，并对已落地代码做审查。本 AI 不修改业务代码，只维护计划与状态证据。
- **方法**：本回合对 git 工作树、各交付物提交状态、pytest 实跑计数做逐项核对，并审查 `agent_runtime/realtime_protocol.py`、`api.py` 网关、`frontend/src/workbench/liveStreamControl.ts`、`voice.py` 的边界与重复逻辑。

### 一、提交记录核对（R14 红线：无更新记录不得标记完成）
- **全部 realtime 交付物均为 untracked（从未提交）或旧提交之上的修改**，仅 AI-D 的 R4/R5 核心（realtime_provider.py / realtime_timeline.py，commit 2c6be35）真正进了历史。
  - untracked：realtime_protocol.py、realtimeProtocol.ts、realtime_metrics.py、liveStreamControl.ts、realtime_bench.py、voice.py；以及 9 个测试文件（test_realtime_gateway_stress / protocol_contract / gateway_faults / metrics / bench_tool / live_stream_control / live_vision / live_vision_alerts / voice）。
  - 旧提交之上修改（M）：realtime_omni.py（AI-D R4 迭代中）、tests/test_realtime_provider.py（AI-D）。
- **结论**：公告表把上述工作标成「已交付/已合并」但缺 commit 记录，违反 R14「没有更新记录的任务不得标记完成」。建议统一加「未提交」标注，并推动各 AI 把对应文件 commit（运行中服务需重启才加载工作树改动）。

### 二、代码审查结论（已落地产物）
- **R11 问题清单 #3/#4/#5 已全部修复并验证**：
  - #3 信封不可被载荷改写：`realtime_protocol.py:server_event` 现用 `if key not in {"v","type","sent_at"}` 过滤（第 75-76 行）；`test_server_event_payload_cannot_clobber_the_envelope` 通过。
  - #4 布尔序号拒绝：`parse_binary_packet` 现 `isinstance(sequence, bool)` → 丢弃（第 47 行）；`test_bool_sequence_is_rejected_even_though_bool_is_an_int_subclass` 通过。
  - #5 observations 类型校验：网关 `api.py:2431-2434` 仅收 `list[str]`，非 list 直接置空（与 `test_non_list_observations_are_dropped_from_the_wire` 一致）。
  - 实测：`tests/test_realtime_protocol_contract.py` **51 passed in 0.07s**。
- **无有害重复逻辑**：`liveStreamControl.ts`（前端自适应发送/相位/在途账本）与 `realtime_metrics.py`（后端指标注册表/直方图）是不同层、不同职责，不构成重复实现。
- **voice.py 与 api.py /api/voice/* 是正常分层**（逻辑 vs HTTP 端点），非重复。

### 三、重复完成风险（必须立即澄清）
1. **⚠️ R2 语音已在树但 AI-C 表为「待分派 0%」**：`voice.py` + `api.py` 的 `/api/voice/status|transcribe|speech|dialogue` 端点 + `tests/test_voice.py`(3 collected) 均已存在且 untracked，但非 AI-C 提交（疑似主代理/P0 线代做）。AI-C 若按表开工 R2 将重复造轮子。→ 立即确认 R2 归属，把已落地产物记到 AI-C 或显式改派。
2. **⚠️ api.py 多写入者争用**：R0/R3 独占网关（`/api/vision/live-stream` 由 /root 写）与另一 AI 的 cockpit/voice/profile 端点（约 +800 行）落在同一文件。虽区域不同，但同文件并发编辑 = 高合并冲突/stat-dirty 风险。→ 提交前由 /root 与其他改动者对齐分区，或拆分模块文件。
3. **⚠️ R8/R9 与 P0/P1 线重叠**：R9 资源/安全/鉴权/清理 与 P0（enterprise_sandbox / process_runner / 命令预算 / 确认门）重叠；R8 工具上下文与 cockpit/agent 线重叠，但均非 AI-E 提交。→ 明确 P0/P1 产出是否计入 R8/R9，避免重复或遗漏。

### 四、接线缺口（影响 R12 联验）
- **R4/R5/R10 三模块在 api.py 中均无 import**（grep 确认）。网关仍是 HTTP 抽帧分析，原生 realtime 模型链路未接通。AI-D 已交付 provider/timeline，但与 /root 的网关接线存在 handoff 缺口 → R12 真机联验前必须先完成 R3↔R4 接线。

### 五、测试环境缺陷（影响进度证据可信度）
- **AI-B R1+R6 套件未全绿**：`tests/test_live_stream_control.py` 实跑 = **1 passed / 2 ERROR**，2 项因 `PermissionError: D:\Temp\pytest-of-h'h'h 拒绝访问`（pytest 临时目录默认落到 D:\Temp 且不可写），属环境问题非逻辑错误。公告「3 passed」在本机不成立。→ 用 `--basetemp` 指向可写目录（如 `D:/Temp2`）重跑确认。

### 六、下一步（R14 推动项）
1. 各 AI 把 untracked 交付物 commit，公告表补「未提交」标注。
2. 澄清 R2 voice 归属，消除 AI-C 重复风险。
3. 协调 api.py 分区提交，降低 stat-dirty 冲突。
4. /root 完成 R4/R5 接入 api.py 网关，解锁 R12。
5. 用可写 basetemp 重跑 AI-B 套件，拿到真实全绿证据。

- `FileTreeNode.vue` 的业务标签默认收起，只在悬停、选中或 AI 定位时展示，保留 Git 状态点、文件类型和右键操作。
- `AutonomousCockpit.vue` 增加固定的“目标 / 执行 / 预览 / 验收”流程轨道，按工作流状态高亮当前阶段，并在窄屏下自动变为两列。
- `useMessageRender.ts` 与 `ChatDock.vue` 为工具时间线增加搜索、读取、修改、验证等阶段分组提示，保留原有 SSE trace、折叠详情和状态判断。
- 验证：前端 `npm run typecheck`、`npm run build`、仓库 `git diff --check` 均通过。构建提示仍为既有的 `/static/session.js` 非 module 脚本和大 chunk。

# 2026-09-29 弹层视觉统一与动作分组细化

- 工具时间线分组只在新的工具动作阶段开始时显示标题，连续的工具返回和复核记录归入当前阶段，避免每条事件重复占用空间。
- 弹层统一工作继续保持展示层范围；现有 `AppDialog`、设置、GPU、运行台、策略和历史弹层的业务接口及关闭/审批行为未改动。
- 最新验证：前端 `npm run typecheck`、`npm run build`、仓库 `git diff --check` 均通过，构建提示仍为已存在的脚本类型和大 chunk 提示。

# 2026-09-29 统一弹层壳与开发舱区域

- `style.css` 增加工作台公共间距、控件高度、弹层圆角、阴影、动效和状态胶囊 token；设置、历史、GPU、权限、运行台和通用确认弹层统一使用 `wb-modal-backdrop` / `wb-modal-shell` / `wb-modal-head`。
- GPU、运行台、设置列表、开发舱证据卡统一接入 `wb-card`；运行状态接入 `wb-status-chip`，保留各组件原有状态颜色和业务动作。
- `AutonomousCockpit.vue` 在真实内容区域标出“01 目标 / 02 执行 / 03 预览 / 04 验收”，目标输入、执行记录、实时预览和审批验收各自有清晰的区域边界。
- 验证：前端 `npm run typecheck`、`npm run build`、仓库 `git diff --check` 通过。构建只保留既有的 `/static/session.js` 非 module 脚本和大 chunk 提示。

# 2026-09-29 全部弹层入口接入公共壳

- 图谱、流程图、差异预览、任务生成、工作流审批、模型设置、素材预览、引擎连接、运行游戏、会话历史和工作流历史等弹层也接入 `wb-modal-backdrop` / `wb-modal-shell`。
- 弹层仍保留各自尺寸、内容布局和业务关闭逻辑，公共壳只统一遮罩、层级、圆角、阴影、动效和可访问性标记。
- 最新 `npm run typecheck`、`npm run build`、`git diff --check` 均通过。

# 2026-09-29 开发舱四区域落地与工具段落折叠

- `AutonomousCockpit.vue` 将开发舱内容明确拆为可访问区域：目标、执行、预览、验收；执行记录、实时预览、审批队列和验收报告分别归位，空任务状态也会保留区域占位。
- `ChatDock.vue` 将连续工具调用收进“本轮任务动作”段落，摘要显示“搜索 → 读取 → 修改 → 验证”等阶段链和记录数，展开后仍可查看每条原始事件。
- 本地 `?demo=1` 预览已检查：四个区域和“更多”菜单均能正常呈现；后端未启动时页面只显示既有 HTTP 500 离线提示。
- 最新 `npm run typecheck`、`npm run build`、`git diff --check` 均通过。

# 2026-09-29 前端收尾审计

- 核对真正的弹层与对话框入口，均已接入公共弹层壳；顶栏下拉菜单和选区工具条保留各自的轻量浮层样式。
- 修复开发舱窄屏样式被后续规则覆盖的问题，并让 760px 以下的预览区和协作区按内容高度顺序排列，避免重叠。
- 在本地演示页以 700px 视口实测：开发舱为单列，两区无重叠，页面无横向溢出。演示页未连接后端时仍显示既有 HTTP 500 离线提示。
- 最新 `npm run build`（含类型检查）和 `git diff --check` 均通过；构建提示仍只有既有的非 module 脚本和大 chunk。

# 2026-09-29 模型切换与设置入口修复

- 修复 `ModelSettingsDialog.vue` 首次按需挂载时没有执行配置回填的问题；现在打开弹窗会立即显示服务端已保存的 provider、模型名、能力和预设，不会再回到 `mock`。
- 修复回填过程中 provider 监听覆盖具体模型名的问题，已验证 `DeepSeek · deepseek-flash` 正确显示。
- 新增 `ModelSwitcherPopover.vue`：对话栏模型芯片只负责在已保存预设之间快速切换；切换后即时刷新当前模型和会话状态。
- 统一设置新增“模型”分组，新增模型、修改接口/Key/上下文/能力等完整操作从“设置 → 模型 → 打开模型参数设置”进入。
- 本地真实工作台已验证：模型芯片显示 DeepSeek，切换面板能读取当前模型；模型参数弹窗首次打开正确回填 DeepSeek；前端 typecheck、build、`git diff --check` 通过。
- 补强自动记忆：`/api/config` 成功切换真实模型后自动生成或复用同配置预设；旧版本已选中但未入预设的模型，在首次打开切换器时补录。真实项目的 `DeepSeek · deepseek-flash` 已补录且在切换列表显示“使用中”。
- 预设切换增加当前项目厂商密钥回退，旧配置从其他模型切回时不必重新填写 Key；密钥仍不写入状态 JSON。模型配置定向测试 22 项通过（当前 Windows Python 测试进程曾输出一次 access violation 诊断，pytest 最终报告 22 passed，需在稳定环境复核）。
- 当前运行中的桌面服务尚未重启，已把 godot_sample 项目现有 DeepSeek 厂商密钥复制到新预设的项目密钥槽位，使它在旧进程里也能直接切换；服务下次重启后新的自动记忆和密钥回退代码正式生效。
- 切换器的“打开设置”已实测进入统一设置的“模型”分组；当前预设显示 1 个，参数弹窗再由该分组打开。最终前端 build、`git diff --check`、后端 `py_compile` 通过。
# 2026-09-29 项目 PDF 生成与等待状态修复

- 修复项目化聊天的后台执行线程不继承 `ContextVar` 的问题：Agent 现在始终使用本次请求选中的项目根目录，避免把 Godot 项目请求串到 DocMind 自身目录。
- 对“根据当前项目生成介绍”增加项目取证约束：必须使用当前项目内至少两个真实来源文件，正文至少四章、700 字；越界来源、缺失来源或过短内容会被工具拒绝。
- 改进 PDF 排版：中文字体、标题层级、分隔线、页脚页码、段落间距与分页保持一致；生成结果会附带来源文件和验证信息。
- SSE 心跳改为注释型 keep-alive，不再反复向对话写入“已进入模型处理 / 模型仍在处理”等等待提示；前端只保留一个旋转状态指示器。
- 新增项目路由与项目介绍回归测试。`.venv` 测试：35 passed；前端 `npm run typecheck`、`npm run build` 通过，构建仍只有既有的 `/static/session.js` 非 module 脚本和大 chunk 提示。
- 已依据 `D:\WorkBuddy\godot_sample` 的 `project.godot`、主场景、玩家/敌人/HUD/数值脚本和导出配置生成并渲染检查 `D:\WorkBuddy\godot_sample\artifacts\StarVoyager-项目介绍.pdf`（2 页，216267 字节）。

# 2026-09-29 项目上下文后台线程审计

- 继续检查后发现并行工具批次和通用 DAG 编排器也会创建线程；已为每个线程复制当前 `ContextVar`，避免多个项目并行时 `read_file`、`search_code`、MCP 或其他项目工具回落到全局根目录。
- 开发舱的规划器、任务执行器、合成器和重规划器现在按持久化工作流的 `project_root/project_id` 绑定上下文；项目工作流恢复或用户切换项目后仍使用原项目。
- 项目工作流证据在已有项目根时不再把全局 DocMind 知识库作为默认证据源，避免规划阶段再次混入无关产品文档。
- 新增编排器上下文回归测试；后端定向测试 135 项通过，Python 语法检查通过，前端 typecheck 通过。

# 2026-09-29 R0/R3 主代理收尾

- 收紧 `agent_runtime/realtime_protocol.py`：`sequence` 拒绝布尔值；`server_event()` 保护 `v/type/sent_at` 信封字段；`compact_error()` 支持携带会话 id。
- 收紧 `api.py` 实时网关：握手后的协议、媒体和音频错误均带当前 `session_id`；模型返回的非列表或非字符串 `observations` 会折叠为空数组，保持前端 `string[]` 契约。
- 恢复持续视觉帧的结构化异常链路：实时视觉提示要求 `observation/anomalies` JSON，并通过已有 `parse_live_vision_result()` 校验后返回，异常候选仍受类型、证据和置信度约束。
- 严格项目桌面帧入口已验证会传递 `strict_project=True`，缺少登记宿主时不会回落到默认窗口。
- 验证：`pytest tests/test_realtime_protocol_contract.py tests/test_realtime_gateway_faults.py tests/test_realtime_gateway_stress.py tests/test_realtime_metrics.py tests/test_realtime_provider.py tests/test_live_vision.py tests/test_live_vision_alerts.py -q -p no:cacheprovider` → **196 passed**；`py_compile`（协议、视觉异常解析器、`api.py`）通过。
- R0/R3 已具备可交接证据；R4/R5 原生 provider 与时间线尚未接入该网关，真实摄像头/麦克风/模型联验仍属于 R12。

# 2026-09-29 R13 兼容模式与发布说明（主代理领取并交付）

- **归属锁定**：R13 由 `/root` 领取并完成；其他 AI 不要重复修改 `api.py` 的 `/api/vision/realtime-status`、`AutonomousCockpit.vue` 的实时模式提示、`docs/realtime-compatibility.md` 或 `tests/test_realtime_compatibility.py`。
- 新增 `GET /api/vision/realtime-status`：报告连接前的原生 provider 可用性、当前候选模式和限制；真实会话以 WebSocket `hello.ok.mode` 为准。
- 开发舱实时视觉面板读取连接状态，并在握手后按 `hello.ok.mode` 显示“原生实时”或“兼容抽帧”；明确音频、延迟和自动降级限制。
- 新增 `docs/realtime-compatibility.md`，记录两种模式、降级语义、`audio_not_ready` 边界、状态接口和 R12 真机联验要求。
- 验证：`tests/test_realtime_compatibility.py` **2 passed**；关闭外部 provider 后实时协议/网关/provider/视觉全组 **201 passed**；`npm run typecheck`、`npm run build`、`py_compile`、`git diff --check` 通过。构建仅保留既有非 module 脚本和大 chunk 提示。
- 剩余边界：R12 仍需真实摄像头/麦克风/屏幕共享和真实 DashScope 人声联验；R13 代码与文档已完成。

## 2026-09-29 R14 推动：untracked 交付物分批 commit + R2 归属澄清（AI-H）

- **commit 推动（按所有者显式路径，未用 git add -A）**：本回合把工作树里 15 个 realtime 交付物按负责人分批提交，避免卷走其他 AI 的 WIP：
  - `d9b5922` AI-F：R11 契约/故障/指标测试(94+54) + R10 bench(13) + live_vision 修复（realtime_metrics.py / realtime_bench.py / 7 个测试文件）。
  - `e654a21` AI-A：R0/R3 网关压力测试（tests/test_realtime_gateway_stress.py，11 项）。
  - `875087b` AI-B：R1+R6 自适应发送 + 控制测试（liveStreamControl.ts + tests/test_live_stream_control.py，3 项）。
  - `075e7e6` /root R0：realtime 协议 v1（realtime_protocol.py + realtimeProtocol.ts）。
  - `045364b` /root R2：语音 STT/TTS 桥接（voice.py + tests/test_voice.py）。
  - 刻意未提交：api.py（多写入者纠缠）、AutonomousCockpit.vue（共享大文件）、P0/P1 树（非本任务表范围）。
- **③ AI-B 套件真实全绿**：用 `--basetemp D:/Temp2` 重跑 = 3 passed；此前 2 ERROR 是 `D:\Temp` 不可写的环境权限问题，非逻辑错误，已证伪。
- **① R2 voice 归属澄清（解除重复风险）**：`voice.py` + `api.py` `/api/voice/*` 端点 + `tests/test_voice.py` 经查证是主代理(/root)「语音协作」线实现（HANDOFF 2026-09-29 第 324-330 行），且已 commit 045364b。任务表原把 R2+R7 派给 AI-C，已更新公告：AI-C 转 R7（语音协作 Agent 合并），勿重写已有 R2。重复完成风险解除。
- **新出现的未提交文件（会话中途由并发写入者产生，归其作者）**：realtime_bridge.py、voice_dialogue.py、voiceVisionSync.ts、CockpitModelBar.vue、docs/realtime-compatibility.md、tests/test_realtime_compatibility.py 等。R14 未代提交，已提示各作者自行 commit。
- **剩余阻塞**：api.py 多写入者争用仍存；R4/R5/R10 接线已改派 AI-F（见任务表 R4/R5/R10 行），待其完成原生实时链路接入后 R12 方可联验。

## 2026-09-29 R14 代码审查结论（AI-H，针对 5 笔新提交）

审查范围：生产代码（非测试）逐文件 review。提交清单 d9b5922 / e654a21 / 875087b / 075e7e6 / 045364b。

- **045364b R2 `voice.py`**：STT/TTS 桥接，纯 `urllib`、零第三方依赖，逻辑正确 ✅。
  - ⚠️ 缺陷 A（中）：同步 `urllib.request.urlopen` 若在 FastAPI async 端点内调用会**阻塞事件循环** → 应改用 httpx 异步或 `run_in_executor`。
  - ⚠️ 缺陷 B（低）：`Content-Type: audio/webm` 硬编码，与实际上传文件(mp3/wav)不符；`urlopen` 的 `URLError/HTTPError` 未捕获，调用方须兜底。
  - 🔴 接线缺口：`045364b` 仅含 `voice.py` + `test_voice.py`，**committed `api.py` 无 voice/realtime 任何引用** → `/api/voice/*` 端点未落地，功能不可达。端点要么缺失、要么卡在并发写入者的未提交 `api.py` 改动中。
- **075e7e6 R0 `realtime_protocol.py` + `realtimeProtocol.ts`**：协议信封权威化、拒绝布尔 `sequence`、`server_event` 保护 `v/type/sent_at` ✅。
  - ⚠️ 缺陷（中）：`server_event()` 的 payload 过滤仅排除 `{v,type,sent_at}`，**未排除 `sequence/captured_at/session_id`** → provider 可在 payload 内注入 `session_id` 覆盖可信值。建议排除集扩到信封键全集。
- **d9b5922 R10 `realtime_metrics.py` + `realtime_bench.py`**：线程安全(RLock)、输入校验拒 bool/NaN/inf；bench 用 `patch.object` 不改 `api.py`，边界诚实 ✅。
  - ⚠️ 观察（低）：`snapshot()` 每直方图 `sorted()` O(n log n)，环缓冲有界可接受；scope 无 TTL/淘汰 → 长驻服务 session 维度可能无限增长。
  - 注：同提交含 `AutonomousCockpit.vue(+798)`/`CockpitApprovalQueue.vue(+143)` 生产改动，本次仅深审 metrics/bench 两个实时核心文件，大组件未逐行审。
- **875087b R1+R6 `liveStreamControl.ts`**：纯逻辑、背压分级、ledger 正确丢弃旧帧、未触碰 R0 冻结字段 ✅；`classifyLivePhase` 5s 启发式合理但未实测。
- **e654a21 R0/R3 压力测试**：11 项，import 现有网关、未改业务代码 ✅。

跨提交结论：
- **重复完成防护**：R2 voice 确认归 /root（非 AI-C），AI-C→R7，无重复；5 笔交付均有 commit，公告表已同步。
- **接线缺口（阻塞 R12，已有 owner）**：(a) R2 语音 `/api/voice` 路由未提交（045364b 仅含 voice.py+测试），仍缺归属，建议并入手；(b) R4/R5/R10 `realtime_provider/timeline/omni/metrics` 未 import 进 `api.py` 网关 —— **已改派 AI-F 认领并开发中**（canvas 公告表 AI-F 行「已认领勿重复·接线开发中」；策略：逻辑进新模块 `agent_runtime/realtime_bridge.py`、api.py 只留少量调用点压低争用面）；(c) `realtime_bridge.py` 仍 untracked，归 AI-F 自行提交。
- **并发争用**：当前 `api.py` 仍 `M`（他写入者）。R14 不碰业务代码；R4/R5/R10 接线已统一归 AI-F（消除三方交叠），仅剩 R2 voice 端点归属待定，建议并入 AI-F 接线工作避免二次争用。

## 2026-09-29 R14 复审：fdb9e94 / realtime_bench.py / 7d1ce00（用户选 A）

聚焦自上次审查后新增的 3 处改动（realtime 相关面）。

- **`7d1ce00` `realtime_omni.py`**：纯 docstring/注释修正——`availability()` note 与模块文档改为「音频输入被账号（实时服务未开通）而非适配器阻断」。无逻辑改动 ✅ 安全。
- **`realtime_bench.py`（未提交 +30/−3）**：防御性增强 ✅。运行期 `patch.dict` 临时清空 `DOCMIND_REALTIME_PROVIDER`（=`realtime_provider.DEFAULT_PROVIDER_ENV`）强制走兼容抽帧通道，避免原生通道不产生 `video.observation` 导致挂死 + 避免真连外部服务产生费用；`_session()` 增加 `hello.ok.mode == MODE_SAMPLED` 断言（fail-fast）。逻辑自洽：清空 env → 网关 `resolve()` 回落 sampled-frames → 断言通过（已核对 `realtime_provider.resolve` 用 `os.getenv(...,"").strip().lower()` 处理空串）。
  - ⚠️ 新依赖：bench 现 `import agent_runtime.realtime_bridge`（`realtime_bridge.py` 仍 untracked，AI-F WIP）。clean checkout 跑 bench 会 import 失败 → 建议 AI-F 提交 bridge 时一并提交 bench。
- **`fdb9e94` `AutonomousCockpit.vue`(+1834/−72)+HANDOFF**：R1+R6 前端接线（自适应码流控制 + 打断 UI 接入开发舱）。
  - 复用 R0 协议助手 `encodeVideoFrame/parseRealtimeServerEvent/realtimeHello/realtimeCancel`（`realtimeProtocol.ts`，已审）与 R1+R6 `liveStreamControl.ts` 的 `createAdaptiveSender/createInFlightLedger/classifyLivePhase/describeLiveCapabilities/appendCaptionTurn`（已审）✅。未发明新线字段，尊重 R0 冻结范围。
  - 引用符号全部已定义/导入：`liveLedger=createInFlightLedger()`(150)、`liveVisionModeLabel`(133)、`liveVisionLimitations`(135)、`describeLiveCapabilities/appendCaptionTurn`(`liveStreamControl.ts:173/183`)、`TimedObservation`(`import type` from `voiceVisionSync`)。✅ 无未定义引用。
  - 清理卫生良好：stop 清 timer/socket(`session.close`)/reset `liveAdaptive`/清 frames+sequence ✅；尊重 `document.hidden` 暂停发送 ✅；打断走 `realtimeCancel` ✅；`hello.ok` 处理 mode/capabilities/limitations ✅。
  - ⚠️ 耦合 untracked WIP：cockpit `import type { TimedObservation } from '../voiceVisionSync'`（`voiceVisionSync.ts` 未提交）。仅类型导入（运行时无耦合），但 cockpit typecheck 依赖该未提交文件 → 建议 `voiceVisionSync.ts` 与 cockpit 一并提交。
  - 范围说明：本次复审聚焦 realtime/adaptive/interrupt 接线（R14 相关面），未逐行审全部 +1834 行（含审批队列等非实时 UI）。

**复审结论**：三处改动质量良好，无阻断性 bug；两处耦合到 AI-F 未提交模块（`realtime_bridge.py`、`voiceVisionSync.ts`），建议 AI-F 收尾时一并提交，避免 clean checkout 断链。R14 不碰业务代码。

## 2026-09-29 R14 复审 A2：33a8542 / 748ab36 / 7636c81（用户选 A2；评审中途状态又前进到 7636c81）

- **`33a8542` R4 适配器修正**：基于重测把断连归因为「commit/cancel」，后被 `7636c81` **推翻**——真因是模型将下线而非账号。该提交代码本身合理，但结论已被 `7636c81` 覆盖（勿据此判断）。
- **`748ab36` R13 字幕/能力 UI** ✅：
  - `liveStreamControl.ts` 新增 `appendCaptionTurn()`（model.delta/audio.transcript 归约成有界字幕时间线；final 整条替换当前回合防重复）+ `describeLiveCapabilities()`（hello.ok 能力中文标签；非数组返回空串，兼容抽帧模式）+ `LiveCaptionTurn` 类型。逻辑清晰、有界（limit=8），测试 44 节点断言。
  - cockpit：hello.ok 渲染能力/限制（含 `degraded_to` 回退提示），model.delta/audio.transcript 接字幕，stop/start 重置；`import type` 引入 `LiveCaptionTurn`。仅触 liveStreamControl.ts/cockpit/test/HANDOFF，未碰 api.py/bridge ✅ 尊重边界。
- **`7636c81` R4 真机端到端（HEAD，推翻上轮）** ✅ 高质量：
  - 真因：**`qwen-omni-turbo-realtime` 将于 2026-10-10 下线且已半停用**（握手正常、音频进流约 2s 后踢人零业务事件）；切到现行入口 `qwen3.8-omni-flash-realtime` 一次打通。账号额度 100% 充足，与权限无关。
  - 改动：DEFAULT_MODEL/DEFAULT_VOICE 改；`capabilities()` 恢复 `CAP_INTERRUPT`（当前模型 commit/clear/cancel 均安全）；`commit()`/`interrupt()` 恢复真实发送（verified safe）；`availability().verified="end-to-end"`。
  - **修复三处真实映射 bug**：① 转写增量走 `conversation.item.input_audio_transcription.delta`，早期仅 `stash` 有值 → 回退取 `stash` 防丢首词；② `error` 是嵌套 `{error:{code,message}}`，此前读顶层取不到 → 改读嵌套；③ `response.done` 的 id 在嵌套 `response.id`，此前 `response_id` 恒空 → 改读嵌套。
  - 新增 `speech_started/stopped → STATUS(listening/thinking)` 带 `audio_start_ms/audio_end_ms`，供 R5 时间线对齐。
  - 测试 27→31 项全绿；docstring 诚实自洽。
  - ⚠️ 过程风险（非代码）：`realtime_omni.py` ~1 小时内经历 7d1ce00→33a8542→7636c81 三轮互相推翻的结论。根因是未在「正确（现行）模型」上一次性真机验证就下结论。建议后续真机验证固定用 `qwen3.8-omni-flash-realtime` + 已验证音色（Jennifer/Ryan/Katerina），避免反复横跳。当前终态正确，仅作复盘。
- **新出现 untracked**：`verify_realtime_live.py`（实时真机验证脚本）、`tests/test_realtime_context.py`、`tests/test_realtime_compatibility.py`、`tests/test_realtime_gateway_bridge.py`、`docs/realtime-compatibility.md` —— 均归其作者（AI-D/AI-F），R14 未代提交。

**A2 结论**：`33a8542` 被 `7636c81` 覆盖；`748ab36` 良好；`7636c81` 为当前权威实现，质量高、修复真实 bug、无阻断问题。R14 不碰业务代码。

## 2026-09-29 R14 核验：用户称「R4/R5/R10 好像完成」

核验结论：**代码已在工作树落地，但尚未提交 → 按 R14「无记录=未完」铁律，不能标记为完成。**

- 工作树实情：`api.py` 已 `M`（未提交），且已接入 `realtime_bridge`：`from agent_runtime import realtime_bridge`(111)；`/api/vision/live-stream` 处理器内 `bridge = realtime_bridge.SessionBridge(project_id)`(2692)、`realtime_bridge.status_snapshot`(2803)、`timeline_snapshot`(2007)；`from agent_runtime.realtime_provider import describe, resolve`(2465)。R4/R5/R10 接线**实现存在**。
- 但：`realtime_bridge.py` 仍 `??` **untracked（未提交）**；`api.py` 改动未提交；`tests/test_realtime_gateway_bridge.py` 也 `??` 未提交 → 整条链路无任何 commit 记录。
- 风险（与之前预警一致）：① clean checkout 跑 `api.py`/bench 会因 `realtime_bridge.py` 缺失而断；② `api.py` 是并发写入热点（`M`），AI-F 提交须用 **pathspec 只加 `api.py` 中自己的接线 hunk**，避免卷走其他写入者的 WIP（参照 `2c6be35` 的踩坑）。
- 建议：AI-F 先把 `realtime_bridge.py` + `tests/test_realtime_gateway_bridge.py` + `api.py`（仅其接线部分）做 pathspec 提交；提交后 R14 再复验（跑 bridge 测试 + 确认原生通道下 `hello.ok.mode` 正确）方可翻状态。
- 重复完成检查：R4/R5 实现线归 AI-D、R4/R5/R10 接线归 AI-F，当前无第二人动 `realtime_bridge.py`/api.py 接线 → 无重复认领。

## 2026-09-29 R14 状态更新（20:01，用户问「现在呢」）

自 19:48 后 AI-F 完成**收口提交批次**（6 笔）：`503e27e`(后端与运行时，含 `realtime_bridge.py` +524 / `api.py` +1319)、`c19aacf`(前端工作台)、`e35ce8e`(测试套件，含 `test_realtime_gateway_bridge`)、`5f1209c`(HANDOFF/设计文档)、`326da8e`(基准模块与验收脚本补遗)、`02fc97b`(收口记录 + HEAD 干净检出验证)。

- **R4/R5/R10 接线：现已提交 ✅**（原「未提交」旗标解除）。`realtime_bridge.py` 入库、`api.py` 接线入库、bridge 测试入库。按 R14「无记录=未完」铁律，现可翻状态为「实现完成·已提交」。
- **R12 真机验收（R4 provider 侧）：未全绿，2/6 指标失败**（据 `docs/realtime-r12-acceptance-20260929.md`, 19:59）：
  - 通过：首响应 3175ms、模型延迟 204ms、打断响应、断线恢复。
  - **未通过**：持续响应 1/3 轮完整（turn-2/3 零输出）、错误率 43.5%（37/85）。
  - 报告诚实限定为「仅 R4 provider 侧」；设备采集 + 驾驶舱体验调优属 R12 负责人范围，未覆盖。
  - 结论：R4/R5/R10 代码交付完成，但端到端体验（持续响应稳定性、错误率）仍欠账，R12 未闭环。
- **仍有的未提交改动**（非阻塞，疑为另一写入者/AI-F 收尾补丁）：`realtime_protocol.py`(+3)、`realtime_provider.py`(+2)、`tests/test_realtime_protocol_contract.py`(+9)、`tests/test_realtime_resource_security.py`(+54)、`verify_realtime_acceptance.py`(+23)；untracked `docs/realtime-r12-acceptance-20260929.md`（R12 证据，建议提交）。
- 重复完成检查：R4/R5 实现 AI-D、接线 AI-F，整链已入库，无第二人重复认领；R12 验收由 AI-F 出证据、设备/UX 部分归 R12 负责人，边界清楚。

## 2026-09-29 R14 代码审查结论（20:26，收口批次 + R9 守卫 + 三项用户指派修复）

本轮 R14 审查覆盖 HEAD=`ef9f0ed` 之前的实时链路新提交代码，结论：**无阻断性 bug**。

### 审查范围与逐项结论
- **`agent_runtime/realtime_bridge.py`（503e27e，R4/R5/R10 接线层，525 行）✅**
  - `api.py` 的 14 个调用点与 `realtime_bridge` 公开签名完全对齐（`SessionBridge(project_id)` / `bind_session` / `provider` / `start` / `capabilities` / `native` / `hello_fields` / `next_events(timeout=)` / `take_audio` / `send_frame` / `note_frame` / `note_observation` / `interrupt` / `note_model_failure` / `close`，及模块级 `timeline_snapshot` / `status_snapshot`），无签名不匹配类 bug。
  - 网关自有事件（`hello.ok` / `session.closed`）在 `next_events` 内按线上事件名拦下（记账不转发）；`EVENT_DONE` 改为补发 `model.delta + final:true + 空文本` 收口标记，与前端 `appendCaptionTurn` 幂等语义一致。
  - 音频 `bytes` → base64 在 `wire_events` 统一处理，杜绝 `send_json` 抛 `TypeError`。
  - 指标作用域按**网关 `session_id`** 记账（非适配器 `rt-...`），`close()` 走 `drop_session` 回收；时间线在最后会话断开时 `release_timeline` 释放 —— HANDOFF 记录的「无界增长雷」已堵死。
  - **线程安全已具备**：`MetricsRegistry` / `RealtimeTimeline` 所有写路径均持锁（`threading.RLock` / `Lock`），`pump_provider` 经 `asyncio.to_thread` 与事件循环并发写指标/时间线不冲突。
- **`api.py` `live_vision_stream` 网关接线（503e27e，2549–2814）✅**：双通道（原生/抽帧）逻辑正确；网关自有事件不二次下发；`hello.ok` 模式字段随握手上报。
- **`6832251`（R9）三处 provider 转发守卫 ✅**：`interrupt()` / `take_audio()` / `send_frame()` 补 try/except，按语义兜底（cancel 仍回 `cancel.ok` + 报 `interrupt_failed`；音频外抛落回 `audio_not_ready`；视频外抛只降级单帧不翻模式），不动成功路径。验证充分：桥测试 27 passed（新增 3 条各覆盖一处），且反向验证（去守卫 → 3 条全红）。
- **三项用户指派修复（已提交）✅**：视觉点击 `/api/vision/locate-click`(2919) + `/api/vision/execute-click`(2985)；`/api/chat` 复验轮(2039–2118，反馈须属本项目、原文不进 system_context)；`EVENT_DONE` 缺口（`realtime_bridge._end_of_turn`）。均落在提交代码中。

### 观察项（非阻断）
- **P3（信息级）**：`take_audio` 在 `provider.send_audio` 返回前先写 `KIND_TRANSCRIPT` 时间线条目；若 `send_audio` 返回 False，时间线会多记一个「被拒音频分片」（api.py 随后回 `audio_not_ready`）。仅日志轻微过度，不影响线上。

### 后续提交（自 20:01 的 `02fc97b` 之后，HEAD 已推进到 `ef9f0ed`）
- `6832251` 之前已审；新增 `5a89f8d`(仅 HANDOFF +42)、`d923445`(api.py cancel 次序 + 测试收口，即此前标的 10/5 未提交漂移已入库；`test_realtime_resource_security.py` 作者 WIP 一并收口)、`ef9f0ed`(仅 HANDOFF +35)。均为测试/文档/api.py cancel 次序，无新增业务逻辑风险。

### Canvas 公告表
- AI-F 行（line 98）阶段字段已由「接线完成·三项修复完成·已收口提交」改为 **「R4/R5/R10 接线：实现完成·已提交·三项修复完成·已收口提交」**，显式点明状态（canvas 由 canvas 工具管理，不在 git 内）。

### 未提交 / 未闭环（R14 仅跟踪，不碰业务代码）
- 工作树未提交（属其他 lane）：`realtime_protocol.py` / `realtime_provider.py`（R0 独占）、`chat.ts` / `ChatDock.vue`（前端）、`test_realtime_protocol_contract.py` / `verify_realtime_acceptance.py` / `web/index.html`。
- `docs/realtime-r12-acceptance-20260929.md` 仍 untracked：R12 真机验收 4/6 通过、2 项失败（持续响应 1/3、错误率 43.5%）——仍 open，依赖账号开通实时多模态服务。

### 重复完成检查（R14 红线）
- R4/R5/R10 接线 = AI-F 独占（`realtime_bridge.py` + `api.py` 接线），无第二人动同一处；R2 voice 已澄清归 /root（AI-C 转 R7）。整链已入库，无重复认领、无重复完成。

## 2026-09-29 R14 状态更新（20:43，用户问「现在呢」+ 要求补状态并审 0db20ff）

### 自 20:26（ab6d89e）后新增提交
- `0db20ff` fix: 实时链路必须在语音后补静音尾（R12 根因修复）+ `8f32730` docs: HANDOFF 声明 R12 provider 侧范围与静音尾根因。HEAD→`8f32730`。

### R12 验收翻转：4/6 → 6/6（provider 侧）
- 根因：语音后缺静音尾——服务端 VAD 靠尾部静音收句，麦克风在用户停说话后仍运行本应提供尾音，**主动掐断才是 bug**（不补静音 0/2、补 1.0s 静音 2/2）。`0db20ff` 在 `verify_realtime_acceptance.py` 推流后补静音尾（`--tail-seconds` 默认 1.0），对照实验翻转验证。
- 指标全通过：首响应 3171ms、模型延迟 223ms、持续响应 3/3、打断、断线恢复、错误率 0.0%（0/75）。`tests/test_realtime_provider.py` 31 项全绿。

### R14 复审：0db20ff 对 realtime_omni.py 的静音尾改动
- **realtime_omni.py 改动为纯文档（+14 行 = docstring gotcha + commit() 澄清），无运行时行为变更**，安全、准确。实际静音尾逻辑在验收脚本 `verify_realtime_acceptance.py`（`_stream` 每轮后补 `tail*10` 帧静音），harness 修复正确且符合契约。
- **发现（P2，须跟踪的生产缺口）**：R12 报告「对 R3 网关的三条硬要求」第 1 条要求采集端/网关在用户停说话后继续推 ~1s 静音（或保持链路到 `speech_stopped`）。但**生产网关路径 `realtime_bridge.take_audio → provider.send_audio(payload, captured_at)` 不注入静音、也不等 `speech_stopped`**（已 grep 确认 realtime_bridge.py 无 silence/tail/idle 逻辑）。→ 当前 6/6 通过是 harness 验证，真实用户走网关仍可能复现「有转写无回复」。**须 R3/R0（网关）或前端 lane 落实静音尾/等 speech_stopped**，否则 R12 对用户侧不算真正闭环。

### 对 20:26 R14 节的更正
- 原「未提交/未闭环」称 R12 doc untracked、4/6 open → 现已提交（0db20ff/8f32730）且翻转为 6/6，该结论作废。
- 原列 `verify_realtime_acceptance.py` 未提交 → 已在 0db20ff 入库。

### 重复完成检查（R14 红线）
- R4/R5/R10 接线 AI-F 独占；R12 provider 侧修复由同一 lane（0db20ff）落地，无第二人认领。无重复完成。Canvas AI-G（R12）行仍标「待开始」已过时，建议更新（非本回合范围）。

## 2026-09-29 R14 复审：09994cd 回合恢复 + 8cefac0 视频闸门 + R12 设备侧复核（22:24）

### 审计范围（本批次新增，HEAD=84a1330）
- `09994cd` fix: harden realtime turn recovery（realtime_bridge.recover / realtime_omni 重放缓冲+send_silence_tail+recover / api.py stream_broken 重连 / 4 个新增测试）
- `8cefac0` fix: gate native video on audio readiness（AutonomousCockpit.vue：音频就绪闸门）
- 复评 `docs/realtime-r12-device-acceptance-20260929.md`（设备侧未通过）与同批 `47566e8`/`fe1cfdd`/`bd416e9`/`84a1330` 配套。

### 09994cd 结论：回合恢复硬化，质量良好，无阻断 bug
**1. realtime_bridge.recover() ✅**
```python
def recover(self) -> bool:
    if not self.native or self.provider is None:
        return False
    recover = getattr(self.provider, "recover", None)
    return bool(recover()) if callable(recover) else False
```
- 仅做能力探测+委托；`getattr` 取到的是绑定方法，`recover()` 不带参调用正确。`recover()` 自身若抛异常由 api.py 调用方 `try/except` 兜成 `recovered=False`，不向上炸。`self.provider is None` / 非 native 直接 `False`，不会误操作。

**2. realtime_omni.py ✅（线程安全）**
- 重放缓冲：`self._replay_audio: deque(maxlen=MAX_REPLAY_CHUNKS=120)` + `self._replay_lock = threading.Lock()`。`send_audio` 持锁追加 `(bytes(pcm), stamp)`；`clear()` 与 `_translate` 在 EVENT_DONE 持锁清空。边界有界（120 片 ≈ 12s@100ms），不会无限增长。
- `send_silence_tail(seconds=1.0)`：时长 clamp 到 [0,5]，步长 clamp 到 [20,500]ms；每片 `b"\x00"*(16000*2*step_ms//1000)` —— 100ms 片 = 3200 字节，与 test 断言一致；逐片 sleep 推流。数学与契约正确。
- `recover(timeout=8.0)`：持锁取 pending → `close()` → `start()`（失败即回 False）→ `poll(0.2)` 等 `EVENT_STATUS`/`stream_broken`（后者回 False）→ 清空重放缓冲 → 重放 pending 音频。超时与失败路径完整，失败回 False 触发网关关会话。
- **并发安全**：`recover()` 由 api.py 经 `asyncio.to_thread` 调用，调用期间主循环在该 `await` 处挂起，不会与恢复中的 `poll`/重放并发读同一 provider；恢复返回后主循环才继续读。无竞态。

**3. api.py live_vision_stream（09994cd +12）✅**
```python
if (event.get("type") == "error"
        and event.get("code") == "stream_broken"
        and event.get("retryable") is True):
    try:
        recovered = await asyncio.to_thread(bridge.recover)
    except Exception:
        recovered = False
    if not recovered:
        return
```
- retryable 的 `stream_broken` 转重连/重放，WebSocket 保活；恢复失败才关会话。非阻塞（to_thread），不卡事件循环。
- **P3（信息级，非阻断）**：`event.get("retryable") is True` 用严格同一性判断；若 provider 某天发出 `"retryable":"true"` 或 `1`，会漏判而不重连。当前 `realtime_omni._translate` 产出布尔，暂无问题；建议后续放宽成显式布尔化作为健壮性兜底。

**4. 测试 ✅**
- `tests/test_realtime_acceptance.py`（新增 74 行）：`done` 严格成功（有 transcript 无 done → 需重试）、静音尾 20 片（10 语音+10 静音）、`stream_broken` 重试不计致命，离线覆盖充分。
- `tests/test_realtime_provider.py` `test_send_silence_tail_emits_one_second_of_vad_audio`：1.0s → 10 片 × 3200 字节，断言精确。

### 8cefac0 结论：直接消除设备报告「视频先行」顺序阻塞 ✅
- 设备报告原结论：先发视频帧再发音频 → `vendor_error: "Error append image before append audio."`，为真机闭环阻塞项。
- `8cefac0` 在 AutonomousCockpit.vue 增加 `liveNativeAudioReady` / `liveStreamReadyForVideo` 闸门：`sendNativeAudioReady()` 先推 1 个 3200 字节静音音频块（=DashScope 要求的「先有音频 append」），置位后才放行视频；`sendLiveStreamFrame` 在 `!liveStreamReadyForVideo` 时 reschedule 不推帧；`startLiveStream`/`stopLiveVision` 复位标志。
- 结论：从采集端根除了「视频帧先于音频」的协议违例，设备报告中的顺序阻塞在代码层已闭合。

### R12 设备侧复核：代码层两条阻塞均闭合，但真机验证仍 open
- 两条设备阻塞（①静音尾、②视频先行顺序）对应代码均已提交：①静音尾由 0db20ff（验收脚本）+ 47566e8（前端 VAD 尾）+ 09994cd（provider send_silence_tail/recover 重放）三层保障；②视频先行由 8cefac0 音频闸门消除。
- **但** `docs/realtime-r12-device-acceptance-20260929.md` 的「设备侧未通过」结论**作为验证声明仍然成立**：本机 IAB 无可授权摄像头/麦克风/扬声器，无法真正跑通采集→上行→播放→抢话→断线恢复全链路。代码已具备，验证待真实 Windows Edge/Chrome + 设备。
- 对 20:43 我标的 **P2 生产网关缺口（realtime_bridge.take_audio 不补静音尾/不等 speech_stopped）**：现已被前端采集端（VAD 尾 + 音频闸门）在链路源头覆盖，**降级为已解决（resolved）**，不再作为 P2 跟踪。原 20:43 该条结论作废。

### 重复完成检查（R14 红线）
- `09994cd`、`8cefac0`、`47566e8`、`fe1cfdd`、`bd416e9`、`84a1330` 全部 `j77156057-art`（AI-F lane）提交；provider 可靠性三条（/root 行）与 AI-G 设备联验职责边界清晰，无第二人认领同一处。
- **Canvas AI-G（R12）行（line 100）已被同步更新为「provider 侧 6/6·代码三条已补齐·音频闸门已修·设备待验」**，与本复审一致；其上一段（20:43）称「仍标待开始」已过时，现确认无需再改。
- 无重复完成。

### 当前工作树并发状态（R14 仅观察）
- `git status` 现显 `M agent_runtime/realtime_omni.py`、`M tests/test_realtime_provider.py`、`M tests/test_realtime_gateway_bridge.py`、`M frontend/src/workbench/api/chat.ts`、`M frontend/src/workbench/components/ChatDock.vue`、`M requirements.txt`、`M .github/workflows/harness.yml`、`?? .tmp/`、`?? artifacts/` —— 均属其他 lane WIP / 生成物，R14 不碰。
- 本节仅追加，pathspec 提交 HANDOFF.md，不卷入上述漂移。

### 结论
**09994cd + 8cefac0 批次质量良好，无阻断性 bug；R12 provider/网关/前端三层可靠性代码已闭环，设备侧仅剩「缺真机」验证缺口，不阻塞代码收口。**

## 2026-09-29 R14 复审：d3e376c + b78bd52（provider 层音频顺序加固 + 网关 flake 修）（22:40）

### 审计范围（HEAD=537d312 之后新增，现 HEAD=b78bd52）
- `d3e376c` fix: prime omni sessions before video frames（realtime_omni.py send_frame 闸门 + close 复位 + _silence_chunk DRY；tests +53）
- `b78bd52` fix: 首帧前先补音频（文档）+ 修掉网关测试收尾 flake（HANDOFF +42、tests/test_realtime_gateway_bridge.py -19/+56）

### d3e376c 结论：媒体顺序闸门下沉到 provider 层，质量良好，无阻断 bug ✅
- 新增 `self._audio_primed` 标志（__init__=False）；`send_audio` 发送成功后才置位（发送失败保持未置位，下一帧重试）；`close()` 复位（recover=close+start 是全新会话，约束按会话计，不清会重连后再踩坑）。状态机正确。
- `send_frame()`：本会话未送过音频时，先经 `self.send_audio(self._silence_chunk(SILENCE_CHUNK_MS), ...)` 补一个 100ms 16kHz PCM16 静音块，**再**发图像。走 send_audio 而非自拼事件 → 同时进重放缓冲、标记由同一路径置位，与 09994cd 的 recover 重放顺序自洽。
- `_silence_chunk(step_ms)` 抽成静态方法，与 `send_silence_tail()` 共用同一算法（100ms→3200 字节），消除两处漂移。正确。
- **与 8cefac0 前端闸门的关系**：前端已先推 3200 字节静音块置位；provider 层是纵深防御——若前端闸门任何边角漏掉，provider 仍自补。两者不冲突、互补。
- **P3（信息级，非阻断）**：`_audio_primed` 为裸 bool，未走 `_replay_lock`。正常路径 send_audio/send_frame/close 均在同一事件循环或 recover 的 to_thread 挂起期内串行执行，无真实并发；但为与既有加锁风格一致、防未来并发调用，建议并入 `_replay_lock` 保护（或加注释声明单线程假设）。不阻塞收口。
- **测试**：test_realtime_provider.py 新增 4 条（首帧前补音频且顺序 audio<image、音频已先行不再塞静音、只补一次、重连后重新引导），均离线可跑、断言精确。

### b78bd52 结论：flake 根因定位精准、修法不降断言强度 ✅
- **根因（正确）**：`gateway` 夹具 `with TestClient(api.app)` 退出拆事件循环；会话拆除在服务端 handler 异步 finally 里、晚于客户端 socket 退出 → 循环一拆 finally 永不完成 → 时间线/指标作用域永不回收 → 用例只能靠运气（等 20s 也红，因为等的是一个不会来的东西）。
- **修法两处，均未放宽断言**：①夹具在退出 client **之前**等 `active_timeline_projects()==[]`（循环还活着时同步）；②把「收尾三件事」属性挪到确定性层面——直接驱动 `SessionBridge.close()`（同步、次序在源码即 provider.close→release_timeline→drop_session），不再经 WebSocket 赌服务端 finally。
- 经网关的收尾仍由夹具同步；R9 的 `test_provider_close_exception_...` 仍覆盖 close 抛错情形，断言面未缩。验证：该文件连跑 5 次全 27 passed（重负载 93~153s/次），全量 2129 passed / 6 skipped / 0 failed（296s，重负载首次全绿）。
- 媒体顺序的**实现说明文档**已在 b78bd52 落进 HANDOFF（作者自述），本 R14 节为独立复核结论，二者一致。

### 作者已诚实标注的未验证窗口（R14 须跟踪，不视作已闭环）
- 作者自述：AI-G 设备对照实验是在**已建立的会话**里做的；本闸门发生在**首帧**，理论上可能早于供应商 `session.created`。若真机上首帧仍报 `vendor_error`，下一步是把首帧也闸在 provider ready 之后（provider 内跟踪 session.created），而不是继续加静音。
- 即：**代码层顺序防御已做（前端+provider 两层），但「首帧早于 session.created」这一边角在真机尚未验证**。结合 R12 设备侧本就缺真机，此项仍属「代码具备、待真机验证」。

### 重复完成检查（R14 红线）
- `d3e376c`、`b78bd52` 均 `j77156057-art`（AI-F lane）；媒体顺序修复从前端(8cefac0)→provider(d3e376c) 同源收敛，无第二人改同一处。无重复完成。
- Canvas AI-G（R12）行（line 100）现「音频闸门已修·设备待验」与本次复审一致，无需再改。

### 结论
**d3e376c + b78bd52 质量良好、无阻断性 bug：媒体顺序闸门已下沉到 provider 层并与前端闸门形成纵深防御；网关收尾 flake 根因精准、修法不降断言。唯一剩余风险 = 作者已标注的「首帧可能早于 session.created」边角，仍需真机验证，不阻塞代码收口。**
