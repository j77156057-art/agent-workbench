# 多模态验收（视觉 + 日志双信号）落地开工文档 —— 游戏引擎 / EDA 域

> 这是一份**自包含**的开工文档。新对话里没有旧上下文时，读完本文即可独立接着干，不需要再问前置讨论。
> 目标读者：执行本任务的 agent 本人。
> 分支：`feature/multimodal-verification`（已从 `main` 切出）。
> 工作树：`D:/WorkBuddy/rag-agent-multimodal-verify`（本仓根的平行 worktree，避免污染 `D:/WorkBuddy/rag-agent` 主检出）。

> **状态：✅ 全部完工。** M1–M6 六段 + 两个收尾增强（七段端到端样例、EDA 只读验证 API）均已提交，工作树干净，里程碑自有测试 182 passed 无回退。
> 里程碑提交：`1f97005`(M1) → `f0f1f16`/`443f0f6`/`58e1fcc`/`e9c05d3`(M2) → `6dbaf6e`(M3) → `3d608d3`(M4) → `45cdbcd`(M5) → `49f51b5`(M6) → `8bf2f37`(样例) → `1aca0ca`(API)。
> **接续前务必先读 §8「实际落地与偏差」**：本文档诚实地标清了哪些是离线替身验的、哪些真机路径（X11/VNC 取像、真 EDA 报告、真 Unity/Godot 回归、人工审批回滚、前端消费）本机还欠着——别当已验证蒙混，也别重造已完成零件。

---

## 0. 一句话目标与信条

把用户 vibecoding 里反复踩到的"**日志一片绿、脚本全通过，但人一眼看出明显 bug**"这个问题，用**视觉信号 + 结构化日志信号交叉验证**系统地治掉。

信条（所有设计决策都必须服从）：

1. **脚本拿事实，视觉补感知。** 能用 API / report / 文件解析精确拿到的，绝不用眼睛看——视觉又慢又贵又会幻觉。
2. **视觉是"高召回、粗粒度"的感知 oracle**，专抓"没有规则可枚举"的明显错误；结构化信号是"低召回、高精准"的事实。两者**交叉否决**：结构化说"0 violations"、视觉说"屏幕上明显不对" → 判失败，不许因为日志全绿就放行。
3. **确定性 diff 当第一道闸，VLM 只做语义解释。** 先用不会幻觉的像素/SSIM 比对筛掉"明显通过/明显回退"，只有超阈值才花一次 VLM 调用去说"哪里变了"。
4. **视觉结论一律先标"待核实"（`verified:false`）**，要经二次确认（后续帧 / 结构化对偶 / 参考图 diff）才升级成结论。模型的口头汇报不是事实。
5. **拿不准就别动手。** 定位、点击、验收都遵循 fail-closed：证据不足→不下结论、不执行。

---

## 1. 现状架构地图（已存在的零件，务必复用，别重造）

工作树根 `D:/WorkBuddy/rag-agent-multimodal-verify`。

### 1.1 视觉取像
- `screen_capture.py` —— Windows 专用（ctypes GDI + Pillow）。
  - `capture_window(hwnd, frame_grabber=None)`：**注意它已留 `frame_grabber` 注入钩子**（原为测试替身设计）。Linux/X11/VNC 取像只要写一个同签名 grabber 传进来，上层零改动。这是 EDA 取像的接入点。
  - `grab_embedded(project_id, strict_project=)` —— 抓当前项目嵌入的引擎子窗口（多候选取面积最大者）。
  - `encode_frame(raw,w,h)` / `encode_and_save(...)` —— BGRA→最长边1600 JPEG q82→data_url。
  - 安全边界：只能抓"嵌入登记表"里的窗口或前台窗口，不给任意 HWND/PID 入口。

### 1.2 观察路（看图说话）
- `agent_runtime/vision.py`
  - `attach_tool_observation(text, images, current_capability=)` —— **所有工具回传的图必须过这个统一闸门**（`agent.py:6013` 处接线）。三态：
    - `native`：模型本身有视觉 → 原图挂进消息，附注"图片只是观察，不是代码事实"。
    - `harness`：纯文本模型 + 配了 `DOCMIND_VISION_MODEL` → 让兜底视觉模型把图转 ≤6000 字观察文字塞进上下文，**绝不向文本主模型发图**。
    - `unavailable/error`：明说"图已忽略，请依据代码和日志判断"。
  - `analyze_images(...)` —— 底层；有递归防护（兜底模型必须自身 native，否则跳过，见 `vision.py:86`）。

### 1.3 动作路（看图点按钮）
- `agent_runtime/visual_targeting.py`
  - `parse_visual_target(raw)` —— 校验模型给的 `{found,bbox[0..1],evidence,confidence}`；bbox 必须是归一 0~1、confidence≥0.8、evidence 非空，否则拒。
  - `target_fingerprint(raw,w,h,bbox)` —— 目标邻域带 padding 裁剪 → 24×24 下采样字节。**这个"带 padding 裁剪+下采样"逻辑可直接复用做 ROI 提取。**
  - `target_still_matches(before,after,max_mean_delta=18)` —— 均值差阈值。**仅够"点击前复核"，给版图上 1px via 缺失这种做 diff 太钝，别复用这个阈值。**
- `api.py` 里 `propose-click`/`execute-click`（约 3304~3497）：
  - `_visual_target_prompt` 已在 prompt 里钉死"bbox 用 0 到 1"。
  - 执行前三道闸：①用户确认 ②重抓同 hwnd 且目标像素指纹没变 ③审批台账留痕。点后过 `VISUAL_CLICK_SETTLE` 抓 after 图，`parse_visual_verification` 做 before/after 判定。

### 1.4 验收路（真实浏览器，日志+视觉双收，是最像目标的范例）
- `agent_runtime/visual_acceptance.py` —— headless Edge 走 CDP：
  - 截图的**同时**收 console error / runtime exception / 失败网络请求（`_DevTools._handle_event`）。
  - `ok = 页面加载 & 无运行时错误 & 无 console error & 无失败请求`。
  - `resolve_live_url()` **拒绝静默换端口**（"能应答≠是我的项目"，防拍到别人画面报成功=假绿灯）。这是"绿≠对"信条的已落地样板。
  - 进程回收用 Job Object "出生即在作业对象里"，防进程泄漏。

### 1.5 实时流（视频+语音，Qwen-Omni）
- `agent_runtime/realtime_provider.py` —— vendor-neutral 适配器契约；`resolve()` **起不来就响亮降级到 sampled-frames，绝不伪装会话**。能力标志 `audio.in/video.in/text.out/audio.out/interrupt`。
- `agent_runtime/realtime_omni.py` —— DashScope Qwen-Omni Realtime 裸 WS 适配器（无 SDK）。文件头记录了：下线模型假握手坑、音色绑模型、**"别在大声说完当刻停推流，要补 ~1s 静音让 VAD 收轮"**。
- `agent_runtime/realtime_bridge.py` —— 接线层（逻辑已从 api.py 挪出）。
  - 网关自有事件（`hello.ok`/`session.closed`）不许转发；`EVENT_DONE` 丢了要补 `final:true` 收口标记；音频裸 bytes 必须 base64 进 JSON。
  - **discovery 回传**：模型口头汇报的视觉异常 → 转成 `model.observation` 事件，强制 `verified:false`、`source:realtime-model`，去重（45s 内相似度≥0.82）+ 节流（2s）。
  - 模式字段写进了 `hello.ok`，但**前端目前不读**（见 R13，模式说明 UI 待做）→ 降级现在是隐形的。

### 1.6 游戏引擎域（地基已有）
- `engine_adapters.py` + `.trae/skills/engine-adapters/SKILL.md` —— 按 `engine in {godot,unity,unreal}` 注入 skill 文本。**这是域适配器契约的现成模板。**
- `api_routes/engine.py`、`unity_graph.py`、`scene_runtime.py`、`workbench_fs.py`、`gpu_coordinator.py`、`verify_engine_embed.py`、`verify_scene_canvas.py`。
- `regions.py` —— 分区开发 2.0：`regions.json` 声明式、DAG 依赖、每区 `verify` 命令、`dev_changesets` 整体回滚。**第7段回滚直接复用它。**

### 1.7 与"像素级 golden diff"相关的现状（重要）
- `run_golden.py` 是**题库 runner**（跑题产出 results.jsonl 给 `agent_eval.py` 打分），**不是视觉回归**。别混。
- 现有 diff 全是**文本/状态**层面：`verify_scene_canvas.py` 用 `difflib.unified_diff` 逐字节还原、`regions.py` 契约校验。
- 已存在的"基线"文件（可参考其 JSON 形状与阈值思路）：`docs/canvas-contrast-baseline.json`、`docs/skin-states-baseline.json`、`docs/visual-baseline-diff-report.md`。**这些都是 web/UI 层的，运行时引擎视口/EDA 的像素 golden diff 还没有——这就是本任务要补的核心新层。**
- `tests/test_page_action_canvas_pixels.py` 有 canvas 像素检查的雏形，值得翻。

### 1.8 相关纪律文档（开工前先扫一眼，避免和既有约定冲突）
- `docs/verifier-discipline.md`、`docs/transform-script-false-success.md`（治"脚本报成功但没做成"）、`docs/capability-boundary.md`、`docs/game-harness-workflow.md`、`docs/task-r15-realtime-discovery-relay.md`、`docs/realtime-r12-acceptance-*.md`、`docs/integrations.md`。

---

## 2. 七段共用流水线（任何域都走这条）

```
[0] 域注册契约 adapter
     ├─ 结构化真值源(怎么读) ──→ [1]
     ├─ 视觉源(哪个窗口/哪些 ROI) ──→ [2][3]
     ├─ golden 参考存取规则 ──→ [4]
     └─ 验收命令 ──→ [7]

[1] 结构化真值(高精准低召回)      ─┐
[2] 视觉取像 (rgba,w,h)            │
[3] ROI 裁切(只裁视口/canvas)      ├──→ [6] 双信号合并门(三态 met/unmet/uncertain)
[4] 确定性 diff 优先(golden SSIM) ─┤        │
[5] VLM 语义判(仅超阈值 delta)     ─┘        ▼
                                    [7] 验收门 + 回滚(dev_changesets)
```

**第0段·域注册契约。** 定义 adapter，每个工具/引擎声明四样：结构化真值来源、视觉源窗口与 ROI、golden 存储、验收命令。加新域=写一个 adapter，下游六段不动。照 `engine_adapters.py` 的形状做。

**第1段·结构化真值。** 能脚本拿的别用眼睛。产出喂 agent 当主证据。

**第2段·视觉取像。** 出统一 `(rgba,w,h)`。Windows 引擎走 `grab_embedded`；Linux EDA 写 `frame_grabber` 注入版（见 1.1），不改 GDI 那套。

**第3段·ROI 裁切。** 复用 `target_fingerprint` 的带 padding 裁剪+下采样，把视口/canvas/波形区单裁再送模型，**别喂整屏菜单**（省 token、少幻觉）。ROI 定义放第0段 adapter。

**第4段·确定性 diff 优先（本任务核心新增层）。** 新建 `agent_runtime/visual_regression.py`：当前 ROI vs golden 做 SSIM / 区域像素哈希比对，**只有超阈值才升级到 VLM**。明显的通过/回退用不会幻觉的比较器判掉。

**第5段·VLM 语义判（仅超阈值 delta 上跑）。** 走 `vision.attach_tool_observation`/`analyze_images`，输出必须**结构化 JSON**（仿 `parse_visual_verification` 三态），结论一律 `verified:false`。**把发现信号从"句首自然语言前缀匹配"改成强制 tool-call**（当前 `realtime_bridge.DISCOVERY_OPENINGS` 靠句首措辞触发，换说法就漏报，验收不可接受）。

**第6段·双信号合并门。** 第1段事实 + 第5段视觉，合成三态；规则=**任一否决即否决**。结构化全绿但视觉明显不对 → unmet。

**第7段·验收门 + 回滚。** 复用 `regions.py` 的 `dev_changesets` 整体回滚 + acceptance gate。unmet → 自动 revert 到 golden 或升级给人，绝不静默放行。

---

## 3. 分域落地

### 3.1 游戏引擎（接起来即可，地基已有）
- 第1段：Unity C#/反射、UE Python Remote Execution、Godot GDScript 读 scene hierarchy / component 值 / Console 编译错 / Profiler（`unity_graph.py`、`api_routes/engine.py` 已在做）。
- 第2~4段视觉专管三件事：
  1. **viewport 渲染结果**：shader 编译失败变粉红、光照爆白、clipping、粒子漏发——日志/属性看不出来，只有渲染出来才知。`grab_embedded` 截 Scene/Game view。
  2. **视觉回归**：改完重跑同场景同帧号 → ROI 截图 vs golden → SSIM/pixel diff → 超阈值才 VLM 说"哪里变了"。游戏美术/HUD 天然有参考图。
  3. **Play-mode 运行时观察**：把 `realtime_omni` 直播接上，口头发现按第5段改造成 tool-call。
- 第6段：引擎"编译过无警告" + 视觉"一片粉红" → 一票否决。

### 3.2 EDA（基本空白，按段建；先结构化后视觉）
- 第0段：新建 `eda_adapters.py` + `.trae/skills/eda-adapters/SKILL.md`，按 `tool in {vivado,klayout,verdi,quartus,innovus,...}` 注入 skill（教起 tclsh、哪些 report 关键、哪些 warning 是噪声）。
- 第1段（**先做这个，别先上视觉**）：新建 `agent_runtime/eda_tcl.py` 驱动 tclsh/SKILL，把综合/时序 report、DRC/LVS deck、utilization 解析成结构化事实。EDA 真相九成在这层。
- 第2段：Windows `grab_embedded`；Linux/X11 写 `frame_grabber`（X11 `XGetImage`/`xwd`，或 VNC 协议拉 framebuffer），保持 `(rgba,w,h)` 签名。
- 第3~5段专管四类"日志写不全、人一眼看出"的画面，**每类单独 ROI**：
  1. **版图查看器**（KLayout/Virtuoso/CC floorplan）：缺层、朝向反、pin 没连、macro 留白异常、routing congestion 色团、DRC hotspot 密集。
  2. **波形查看器**（Verdi/GTKWave/SimVision）：glitch、X 传播、时序竞争、复位瞬间怪跳变。
  3. **原理图/网表可视化**：看着连了其实没连的交叉、buffer 缺失、符号手性反。
  4. **资源/时序/功耗热力图**：FPGA 利用率红区、slack 分布、拥塞图（本就是给人眼设计的）。
- 第6段结构化对偶：版图截图 ↔ GDSII/OASIS 多边形（`gdstk` 解析）、波形截图 ↔ VCD/FSDB、网表 ↔ CDL/SPEF。规则=**先结构化 diff 定位，对不上再让视觉判语义**；绝不用 VLM"看 track 连没连"。

---

## 4. 里程碑与验收标准（按此顺序建）

- **M1 `visual_regression.py`（先不接域） — ✅ 已完成 `1f97005`**
  - 提供：ROI 裁剪（复用 `target_fingerprint` 思路）、`ssim(a,b)`（纯 numpy 实现优先，避免强依赖 scikit-image；见 5.环境）、`region_hash`（区域精确哈希）、golden 存取（参考 `docs/canvas-contrast-baseline.json` 的 JSON 形状）、`compare(...) -> {passed, score, roi_changed, verdict}`。
  - 验收：单测覆盖"同图 diff=0"、"改动超阈值 diff>阈值→escalate"、"区域像素级小改（1px）被 region_hash 抓到而 mean-delta 抓不到"。
- **M2 接游戏引擎一条链 — ✅ 已完成 `f0f1f16`+`443f0f6`+`58e1fcc`+`e9c05d3`（拆 4 段提交）**
  - 一个真实 Unity/Godot 场景：`grab_embedded` → ROI 视口 → golden SSIM → 超阈值才 VLM。
  - 把 discovery 从"句首前缀匹配"改成 tool-call（新增一个 `flag_anomaly(target,evidence,confidence)` 结构；`realtime_bridge` + 对应 prompt 同步改）。
  - 验收：拿它做 3 次美术/HUD 回归验收，能抓到注入的明显回退。（**偏差见 §7**：真机 3 次回归未在本机执行，用注入替身帧验。）
- **M3 EDA 结构化通路（不碰视觉） — ✅ 已完成 `6dbaf6e`**
  - `eda_adapters` + `eda_tcl.py`，Vivado/KLayout/Verdi 三家的 report 解析跑通。
  - 验收：一个样例工程能拿到"综合后时序/面积/DRC 数"结构化结果。（**偏差见 §7**：真实工具报告未在本机跑，用 baked 报告文本 + 注入 runner 离线验。）
- **M4 Linux 取像 — ✅ 已完成 `3d608d3`**
  - X11/VNC `frame_grabber`，签名对齐 `capture_window`。
  - 验收：注入替身 + 真机各截一帧，形状一致，上层不改。（**偏差见 §7**：真机 X11/VNC 截图未在本机执行，本机无 X11/VNC；仅注入替身 + baked PNG 验形状一致。）
- **M5 版图/波形 ROI golden diff 上线 — ✅ 已完成 `45cdbcd`**
  - 先做"和上次比"（最稳），**别一上来做"看图找 bug"**。
  - 验收：同 testcase 两版图，注入一个 via 缺失能被 region_hash/SSIM 抓到。
- **M6 合并门 + 回滚 — ✅ 已完成 `49f51b5`**
  - 第6段三态合并 + 第7段 `dev_changesets` 回滚接线。
  - 验收：结构化全绿 + 视觉 unmet 的用例，被判 unmet 且可回滚。（回滚回调注入假实现验"可回滚"，真实 `regions.rollback_changeset` 的 `require_approval` 人工门未自动执行。）

- **收尾增强（用户追加"两个都要"） — ✅ 已完成**
  - 七段端到端样例脚本 `eda_verify_pipeline.py` — ✅ `8bf2f37`。
  - EDA 合并门/解析 REST 路由 `api_routes/eda.py`（只读判，不执行回滚） — ✅ `1aca0ca`。

---

## 5. 已知坑与约束（务必读）

1. **`ai_score()` 返回的是 tuple `(score, details)`**，别当 float 用（历史踩坑）。
2. **别复用 `target_still_matches` 的 24×24 均值差≤18 做像素回归**（对 EDA 太钝）；且它在 BGRX 原始字节上算，把无用的 alpha/X 通道也算进了均值——写 diff 时先转纯 RGB。
3. **视觉结论必须 `verified:false` 起步**，别把模型口头汇报当事实直接播给用户/直接触发副作用。
4. **降级要响亮上报**：`hello.ok` 已带 `mode/degraded_to/reason`，但前端不读（R13 待做）。做 M2 时顺手把模式显示补进 cockpit，否则用户不知道画质掉回抽帧。
5. **指标语义别混**：`observation_latency_ms`/`first_audio_ms` 是网关内部延迟，≠端到端体感（端到端在 `realtime_bench.py`）。状态 UI 要分开标。
6. **连续视频很贵**：单槽背压（`note_frame` 里 `_pending_frames` 覆盖记 `frames_dropped`）已封队列；anomaly 检测 1~2fps + 变化再突发即可，不必 30fps。
7. **取像平台**：EDA 多在 Linux/X11 或远端 VNC；`screen_capture.py` 是 Windows-only GDI。走 `frame_grabber` 注入，别硬改 GDI 路径。
8. **坐标空间**：prompt 要钉死归一 0~1；但原生 grounding 模型（Qwen2.5-VL 常吐 [0,1000]/绝对像素）可能无视指令 → 被 0~1 校验拒=假性"定位失败"。加量级识别与 rescale。
9. **别动主检出**：所有工作在 worktree `D:/WorkBuddy/rag-agent-multimodal-verify`（分支 `feature/multimodal-verification`）。主检出 `D:/WorkBuddy/rag-agent` 有未提交改动，别切分支污染。

### 环境备注（本机）
- Shell：Bash 是 Windows 侧类 unix（无 `/mnt/c`、无 `wslpath`）。
- Glob/Grep 可用（ripgrep 正常）；搜代码用 glob 参数排除 `.venv`/`.chroma`/`.git`。
- SSIM 首选纯 numpy 实现（高斯窗），避免引入 scikit-image/opencv 的重依赖与 GPU 冲突；确需可加 `scipy.ndimage`。
- 有 `.venv` 在项目里（Python 项目）。

---

## 6. 给新对话的接续清单

新对话里按此顺序起手，无需前置上下文：
1. `cd D:/WorkBuddy/rag-agent-multimodal-verify`（worktree）；确认在分支 `feature/multimodal-verification`。
2. 读本文件 `docs/multimodal-verification-engine-eda-kickoff.md`。
3. 扫第 1.8 节列的纪律文档 + 直接读关键源码：`screen_capture.py`、`agent_runtime/visual_targeting.py`、`agent_runtime/visual_acceptance.py`、`agent_runtime/realtime_bridge.py`。
4. **M1–M6 + 两个收尾已全部完成并提交**——先读 §8「实际落地与偏差」，看清每段落了什么、哪些是离线替身验的、哪些真机路径还欠着，别当已验证蒙混、也别重造已完成零件。
5. 若要接续，从 §8.3「真机还欠着的」挑一项做（真 X11/VNC 取像、真 EDA 报告回归、真 Unity/Godot 视觉回归、人工审批回滚闭环、EDA 前端消费方）。每完成一段提交一次，前缀 `verify(mm):`（文档/收尾用 `feat(mm):`/`docs(verify-mm):`）。

### 恢复/清理命令（worktree 生命周期）
- 列出：`git worktree list`
- 完工移除：`git worktree remove D:/WorkBuddy/rag-agent-multimodal-verify`
- 只删分支：`git branch -D feature/multimodal-verification`

---

## 7. 提交约定
- 不提交 `.env`、`.docmind_*`、`.chroma`、`*.sqlite3`、`.venv`、`__pycache__`。
- 一个里程碑一个 commit，小而可回滚。
- 代码注释解释"为什么"，沿用仓库现有中文注释风格。

---

## 8. 实际落地与偏差（交接记录 · 本轮全部完成）

> 六段里程碑 + 两个收尾增强均已提交，工作树干净。本节如实记录**每段落了什么、
> 哪些是"离线替身验"、哪些真机路径本机（Windows，无 EDA/Linux/VNC/FastAPI 生产运行）
> 还欠着**，供下一个有真环境的人接续，不当已验证蒙混。

### 8.1 交付物清单（按段）

| 段 | 文件 | commit | 状态 |
|----|------|--------|------|
| M1 | `agent_runtime/visual_regression.py` | `1f97005` | 纯 numpy SSIM + 8×8 区域块哈希 + golden 存取 + `compare` 三态；单测全绿 |
| M2① | `agent_runtime/engine_visual_regression.py` | `f0f1f16` | source 可注入→`crop_roi`→`GoldenStore.compare_against_golden`→VLM 升级包 |
| M2② | realtime discovery → `flag_anomaly` JSON tool-call | `443f0f6` | 句首前缀匹配改严格 JSON（`realtime_bridge` + prompt 同步） |
| M2③/③b | 视觉模式展示归一 + 接线层 | `58e1fcc`/`e9c05d3` | 降级不再隐形；**只做接线不假装挂载**（无 cockpit 消费点则不强挂 UI） |
| M3 | `agent_runtime/eda_tcl.py` + `eda_adapters.py` + `.trae/skills/eda-adapters/SKILL.md` | `6dbaf6e` | Vivado 时序/利用率、KLayout DRC XML、Verdi 仿真解析→`EdaFact`；未知组合 fail-closed |
| M4 | `agent_runtime/frame_grabber_linux.py` | `3d608d3` | X11（ImageMagick `import` PNG）+ 最小 VNC RFB raw；签名对齐 `capture_window`，BGRX 字节布局 |
| M5 | `agent_runtime/eda_visual_regression.py` | `45cdbcd` | 版图/波形/原理图/热力图四类 ROI；`frame_source_from_grabber` 桥 3→4 元组；复用 M2 `check_visual_regression` 零上层改动 |
| M6 | `agent_runtime/verification_gate.py` | `49f51b5` | 三态合并（任一 UNMET→UNMET，否则任一 UNCERTAIN→UNCERTAIN，全 MET 才 MET）+ `acceptance_decision` + `gate_and_maybe_rollback`（回滚只在 UNMET+`auto_rollback`+注入回调时触发） |
| 收尾 | `eda_verify_pipeline.py` | `8bf2f37` | 七段端到端样例：域注册→结构化→取像存 golden→ROI diff→合并门+回滚；CLI 打 4 个合成用例 |
| 收尾 | `api_routes/eda.py`（+ `api.py` flatten 循环加一行） | `1aca0ca` | `POST /api/eda/parse-report`、`GET /api/eda/skills`、`POST /api/eda/verify`——**全只读，`executes_rollback:false`** |

### 8.2 离线替身验的（逻辑成立，但非真机证据）

- **M2/M4/M5 取像**：全部用**注入的假 grabber / baked PNG** 出帧验形状一致与 diff 逻辑。本机是 Windows，无 X11、无 VNC server、无运行中的 Unity/Godot 编辑器 viewport。`(bgra,w,h)` 契约、BGRX 布局、crop/SSIM 通路都单测覆盖，但"真截一帧游戏/版图画面"未做。
- **M3 报告解析**：用 **baked 报告文本 + 注入 `tcl_runner`** 验解析器输出。未在本机真跑 Vivado/KLayout/Verdi（无工具、无 license）。解析器正则按公开报告格式手写，真实工具的版本差异（表头/列名漂移）未经真样本回归。
- **M4 X11**：走 ImageMagick `import -window <id> png:-`。**故意不手搓 XWD 二进制头解析**——本机无法验证头布局，写死 `struct.unpack` 反而造假风险。仅有 xwd、无 ImageMagick 的环境列为已知后续。VNC 只实现 raw 编码单矩形整屏，tight/zrle、多矩形、DES 认证一律 `fail-closed` 返 `None`。
- **M6 回滚**：用**注入的假 `rollback` 回调**证明"被判 unmet 且可回滚"这条验收。真实 `regions.rollback_changeset` 内的 `require_approval` 人工审批门未自动执行（本就不该自动执行，见 8.3）。
- **前端消费**：合并门结果**未接入任何 UI 面板**——当前 workbench 无 EDA 面板消费方（与 M2③"不假装挂载"同一处境）。只交后端 API，不虚构前端接线。

### 8.3 真机还欠着的（下一个有环境的人接续）

1. **真 X11/VNC 取像**：在 Linux 桌面或真 VNC 上截 KLayout/Verdi/GTKWave 窗口，验 `png_to_frame`/`rfb_grab_frame` 出帧与 GDI 字节布局完全一致、上层零改动确成立。
2. **真 EDA 工具报告**：拿真实 Vivado `report_timing`/`report_utilization`、KLayout DRC XML deck、Verdi 仿真日志回归 M3 解析器，补齐版本差异与告警噪声分类。
3. **真 Unity/Godot 视觉回归**：跑 M2 计划的"3 次美术/HUD 回归"，注入明显回退看能否抓到；顺带补 cockpit 的降级模式显示（§5 第 4 条 R13）。
4. **人工审批回滚闭环**：把 M6 的 `gate_and_maybe_rollback` 接上真实 `regions.rollback_changeset` + `require_approval`，验"UNMET→升级给人→人批准→回滚"全链。**HTTP 层永不自动 revert**（fail-closed 信条）。
5. **EDA 前端消费方**：若建 EDA 工作台，用 `vision.attach_tool_observation` 那套 `verified:false` 语境消费 `/api/eda/verify` 结果，视觉结论不得当已核实事实直接播给用户。

### 8.4 测试基线（全离线，`D:/WorkBuddy/rag-agent/.venv`）

- Python：里程碑自有测试集 **182 passed**，无回退（本轮实测）。
  - `test_frame_grabber_linux` 25、`test_eda_visual_regression` 14、`test_verification_gate` 34、`test_verification_gate_reshape` (含)、`test_eda_verify_pipeline` 10、`test_api_eda_routes` 9（TestClient + 桩 ctx）——这 5 个文件单跑 = 92。
  - 另 `test_visual_regression`(M1)、`test_engine_visual_regression`(M2①)、`test_realtime_flag_anomaly`(M2②)、`test_eda_tcl`(M3)、`test_verification_gate_reshape` 单跑 = 90。
- 前端：`node --test` realtimeModeDisplay + Tracker **17 passed**，未受影响。
- 复跑：`cd D:/WorkBuddy/rag-agent-multimodal-verify && D:/WorkBuddy/rag-agent/.venv/Scripts/python.exe -m pytest tests/test_visual_regression.py tests/test_engine_visual_regression.py tests/test_realtime_flag_anomaly.py tests/test_eda_tcl.py tests/test_frame_grabber_linux.py tests/test_eda_visual_regression.py tests/test_verification_gate.py tests/test_verification_gate_reshape.py tests/test_eda_verify_pipeline.py tests/test_api_eda_routes.py -q`。

### 8.5 本轮真机补充验证（Windows 桌面会话 · Godot 4.7.2 + 工作台前端）

- **Godot 原生 SetParent 嵌入（真机）**：`verify_engine_embed.py --godot <4.7.2>` 实机 **82 passed / 4 failed / 2 skipped / 0 未证实**。真机确证：真实启动 → 按 PID 枚举引擎窗口 → `SetParent` 嵌入 → fill/rect 两种几何 → 宿主 resize 跟随重排 → 150% DPI 下严格对齐 → detach 可逆 → 停止无孤儿窗口/进程 → 崩溃看门狗兜底 → HTTP `/api/engine/embed` → UI 调用路径。渲染证据：`docs/screenshots/engine-embed.png`（宿主窗口内的真机渲染面）。
  - 4 项 FAIL 全部收敛到 `[0]` 一条：合成按键 `SendInput` 插入 2 条、自建窗口收到 **0** 条消息——本会话（从服务端 Bash 起的进程）拒绝把合成输入送达交互桌面的消息队列（UIPI/session 隔离），**非产品缺陷**。遮挡已排除：最小化 QoderWork 后"点击点未被遮挡"由未证实转为通过（未证实 1→0）。
  - 按信条：「合成键鼠 → 引擎回显」这一段仍记为**未证实**，不得写成已验证。
- **EDA 多模态验收面板（真机点击路径）**：在完整 boot 的工作台里经真实点击（更多 → EDA 多模态验收）可达、渲染正常，消费 live `/api/eda/*`（parseReport + verify → 正确三态）。面板已复用 `SceneRuntimePanel` 的 dock 惯用法实现**弹窗 + 停靠常驻双形态**（`mode`/`host` prop + `Teleport defer`，同实例切换、绝不重建）。
- **原生桌面壳（pywebview / WebView2）在本会话无法复现**：`desktop.py` 创建的 WebView2 原生窗口初始化失败（`无效的 URI/端口`，换干净 user-data 目录同样失败），`loaded` 事件不触发 → 宿主 HWND 注册不上。**环境限制，非代码问题**。
  - 改走 **Web 导出预览**路（同源 `/play/<token>/index.html`，服务端带 COOP/COEP 启用 SharedArrayBuffer）：真实点击「更多 → 运行游戏 → 导出并试玩」，Godot 导出 WASM（~7s）后在工作台「Web 试玩」面板里**实时渲染**（同源 canvas 采样非空、右侧时间线记到 `__bridge_ready__ [web]`）。证据：`outputs/workbench-godot-embed-preview.png`。
  - 诚实边界：这是 **Web 导出预览**，不等于原生 `SetParent` 嵌入；面板自身也如实标注「浏览器模式下无法嵌入原生窗口，请用桌面端启动 DocMind」。Godot WASM 默认字体缺中文字形，标签中文显示为方框，与产品无关。
