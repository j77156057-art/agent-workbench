# MCP 自动连接 · L0/L1 浏览器自动化注册 —— 架构设计

> 状态：设计已定（架构师），进入实现。
> 范围：仅覆盖「注册 / 取凭证」阶段；发现 / 抽取 / 校验沿用现有链路（`mcp_autoconnect.py`）。
> 关联文件：`mcp_autoconnect.py`、`api.py`（MCP autoconnect 端点）、`frontend/src/workbench/api.ts`、`frontend/src/workbench/components/SettingsView.vue`、`docmind.spec`。
> 行号以本仓库（`<repo>`）为准。

## 0. 两条纠偏
- 行号：本树 `subprocess.Popen` 在 `mcp_client.py:432`、`save_server` 在 `mcp_client.py:352`（更早简报里的 :405/:340 属旧树）。
- **API**：持久会话必须用 `p.chromium.launch_persistent_context(user_data_dir=..., channel="msedge", headless=False)`；`browser.new_context(user_data_dir=...)` **不是合法 Playwright API**。

## 1. 浏览器路线选型

| 维度 | A. playwright `channel='msedge'`（**主**） | B. CDP 直连系统 Edge（回退） |
|---|---|---|
| 依赖 | `playwright==1.55.0`（`requirements.txt` 已钉）+ node driver | `websocket-client`（更小） |
| 打包 | `collect_all("playwright")` 扩 `docmind.spec`；driver 冻结 bundling **未实测（OPEN）** | 更轻，但需自管 `--remote-debugging-port` 的启动/发现 |
| 失败模式 | driver 缺失 → launch 抛错（可捕获 → 降 L2） | 自写 DOM/等待/点击，脆弱面大 |
| 与「复用系统 Edge 不下载浏览器」契合 | **完全契合**（`channel=msedge` 即用系统 Edge），与既有 Node `playwright-core` 约定同源 | 同为系统 Edge，但需自建协议层 |

**结论**：A 为主路径，B 作 A 冻结打包失败时的回退。

## 2. L0/L1/L2 状态机

### 会话字段（`project_state` → `autoconnect_sessions.json`）
`tier ∈ {L0,L1,L2}`、`status ∈ {running,waiting_user,done,failed}`、`user_prompt`(新)、`step`(新)、`provider`(新)、`context_dir`、`resume_token`、`created_at`、`url`。

### 转移
```
start ──► 打开持久上下文到 provider 注册页（限 provenance 域）
  ├─ 可程序化填且无挑战 ─► L0: 填→提交→成功页 ─► 捕获凭证 → secrets_store ─► status=done
  ├─ 命中挑战(验证码/邮箱验证/2FA/支付墙) ─► L1: 填到挑战前停下, 浏览器保活(headful)
  │                                              status=waiting_user, tier=L1, 返 user_prompt+resume_token
  └─ Edge/playwright 缺失 或 站点不支持 ─► L2: 不起浏览器, tier=L2, status=waiting_user, 返 url+note
resume ──► (L1) 用户点继续 → 同一 live context 续跑（或崩溃后按 context_dir 重开持久上下文）→ 回 L0 判定
commit ──► 用户回填 / L1 自动捕获的凭证 → secrets_store.save → status=done
```

### 会话保持选型
用 **persistent context**（`user_data_dir=context_dir`），**不用** `storage_state`：
- 现有 schema 本就带 `context_dir`（`mcp_autoconnect.py:638,670`）；
- 持久上下文保 cookie+localStorage+IndexedDB，跨人工步与崩溃可重开；
- L1 期间**保持浏览器进程存活**（headful，用户要看验证码），另用**进程内注册表**（仿 `mcp_client._SESSIONS`）按 `task_id` 持 live context；磁盘 `context_dir` 作崩溃恢复兜底。

## 3. 端点契约（对齐现有 Pydantic 模型与路由）

- `POST /api/mcp/autoconnect/register/start` — req `{key:str, config:dict, provider:str}` → resp `{ok, task_id, tier:'L0'|'L1'|'L2', url?, note?}`。
  现状恒定 L2（`mcp_autoconnect.py:639,643`）；实现后按判定返 L0/L1/L2。
- `GET /api/mcp/autoconnect/register/status?task_id=` → resp = 会话字典展平 `{ok, task_id, tier, url, context_dir, resume_token, created_at, status, user_prompt?, step?}`。
  ⚠️ 要支持 L1，**必须把 `user_prompt` 持久化**，否则 status 里没有。
- `POST /api/mcp/autoconnect/register/resume` — req `{task_id:str}` → resp `{ok, task_id, tier, status, user_prompt?}`。现状 TODO 桩（`api.py:3396`）。
- `POST /api/mcp/autoconnect/register/commit` — req `{task_id:str, credentials:dict}` → resp `{ok, stored:list[str]}`（**仅 provider 名，不含明文**）。现状已写 secrets_store（`api.py:3401`）。

## 4. 凭证捕获
- **来源**：注册成功页 / 「创建 API key」页内联展示的 token（`<code>` / copy 按钮 / `input[readonly]`）。
- **读取**：由**白名单 DOM 选择器集**读取 `page.locator(sel).inner_text()`；**绝不** `eval` 页面脚本，**绝不**把值回传模型。
- **落库**：直接 `secrets_store.save(root, provider, value)`；**不写**会话 JSON、**不写**日志、`stored` 只回 provider 名。
- **引用**：连接器 `config.env` 写 `@secret:<provider>`，spawn 时由 `resolve_secret_refs`（`mcp_autoconnect.py:541-563`）在注入前解析，明文不落 `.docmind_mcp.json`。

## 5. 安全边界（**绝不自动做**）
**动作 denylist**（命中即停在 L1 由用户点，或降 L2）：付款/订阅/升级、创建或撤销 PAT/长期令牌、修改或删除账号、改邮箱/密码/安全性设置、点 OAuth 授权同意、验证码绕过尝试、安装扩展、下载/运行 exe。

**防 prompt-injection（注册页正文不可信）**：
1. 浏览器动作由**确定性状态机 + 固定选择器 + provider 适配器数据**驱动；**不把页面文本喂给模型**决定动作。模型若参与，只输出受约束的 `{step, selector_kind}`，过同款闸门。
2. 导航**限定**在 provider 可信域或 `cand.provenance.url` 主机（`is_trusted_domain`），**不跟跨域重定向填凭证**。
3. 凭证 / 页面 token **绝不进模型上下文**。
4. 注册流程**只写 secrets_store**，**绝不**自己写 `.docmind_mcp.json`；写盘仍走独立 `confirm`（`user_ack` 闸门）。
   不变量链：唯一真实执行点仍是 `probe_candidate`（`mcp_autoconnect.py:566-588`）与 `confirm→save_server`（`mcp_client.py:352`）；`Popen` 仅 `mcp_client.py:432`。

## 6. MVP 与落地清单

### 第一批（1–2 类流程）
1. 「注册页 → 立即展示 API key」的**单页无验证表单**（纯 L0）：**GitHub PAT**（官方预填深链）、**Figma**、**Stripe 测试键**。
2. 仅被 captcha / 邮箱验证阻断的站点（**L1 停-继续**）：**Notion**、**Slack**。
3. **L2 只给指引**（首版不做自动）：**Brave**（绑卡）、**Google Drive**（Cloud Console 多步 + OAuth）。

### 扩法
provider 适配器注册表（按域名的**数据**文件：注册 URL、字段选择器、提交后 token 选择器、挑战检测关键字）；新增 provider = 加一条数据，**不改状态机**。OAuth Device Flow 作为后续 L0 快路径。

### 落地清单（签名级）
- `mcp_autoconnect.py`：
  - **替换** `browser_register(root,key,cand,provider)->dict` 桩体（`:617-643`），**签名与返回键不变** → drop-in。
  - 新增 `_BROWSER_SESSIONS: dict[str,dict]`（仿 `mcp_client._SESSIONS`）、`_launch_edge(context_dir)`（`launch_persistent_context`）、`_detect_challenge(page)->str`、`_fill_and_submit(page,adapter)->tuple[bool,str]`、`_capture_credential(page,patterns)->str`。
  - 新增 `register_resume(task_id)->dict`、`register_commit(root, task_id, credentials)->dict`。
  - 扩展 `_persist_session`（`:659-676`）新增 `provider/user_prompt/step`；补 `_load_session/_update_session`。
- `api.py`：`register/resume`（`:3396-3399`）桩 → `run_in_threadpool(register_resume, task_id)`；`start`/`commit` 签名不变。
- `frontend/src/workbench/api.ts`：补 `registerResume` 封装（现有仅 start/status/commit）。
- `frontend/src/workbench/components/SettingsView.vue`：加 L0 进度 + L1「点一下继续」按钮（现默认 L2、无轮询/继续 UI）。
- `docmind.spec`：加 `collect_all("playwright")`。

### 平滑性
现桩已具备：探测（`:628,631`）、建 `context_dir`（`:638`）、落会话 L2/waiting_user（`:639`）、返回契约（`:643`）。真实实现保持同一返回契约，仅把「恒定 L2」改为按判定吐 L0/L1，并补 resume；前端读 `r.tier` 且默认 L2，后端异常也安全降级。

## 7. 裁定更新（2026-09-25，QA 复核后）：§5 优先于 §6

**背景**：§5「绝不自动创建 PAT/长期令牌」与 §6「GitHub PAT / Figma 定为纯 L0」自相矛盾。QA 独立验证抓到实现照 §6 做 → `_fill_and_submit` 自动点了「Generate token」，**自动创建出令牌**，违反 §5。

**裁定（lead）**：**§5 优先**。绝不在用户未点「创建」时自动生成长期凭证。

- **需「创建」长期令牌的流程**（GitHub PAT、Figma）→ **L1**：自动**填好**表单（名称 / scopes / 有效期；GitHub 走官方深链），**停在提交前**，由用户点「Generate/Create token」，再点「继续」（resume → 捕获 → 落 secrets_store）。
- **仅「揭示 / 复制已存在凭证」的流程**（**Stripe 测试键**）→ **保持 L0**（无创建动作，可自动）。
- **实现落点**：适配器新增 `auto_submit` 标志（默认 `False`）；`_fill_and_submit` **仅当 `auto_submit is True` 才点击 submit**，否则填完即返回「填表完成、待用户提交」→ 走 L1 停点。GitHub / Figma 设 `auto_submit: False`；Stripe 测试键设 `True`。
- §6 的 MVP 表据此修正：**L0 = Stripe 测试键**；**L1 = GitHub PAT、Figma、Notion、Slack**；**L2 = Brave、Google Drive**。

### 本次不进（列 backlog）
- **N4**：L1 保活浏览器无超时回收（需设计 TTL / 关闭钩子）。
- **模块抽取**：`mcp_autoconnect.py` 已超 1300 行（项目 300 行风格门禁），建议抽 `mcp_browser_register.py`；本增量先不抽，保持可审。
