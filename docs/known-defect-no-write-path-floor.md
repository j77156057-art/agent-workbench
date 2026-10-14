# 写路径缺一层与 root 无关的地板

> 立档：**2026-10-08**。来自 `docs/harness-research-verified.md` §4.2 ③
> （Hermes 的 `HERMES_WRITE_SAFE_ROOT` + 硬地板 denylist）。
> 探针：`tools/mutation/probe_write_denylist_gap.py`，可复算。

## 1. 一句话

我们的写约束**全部依赖 `code_root`**（一处根目录），**没有一层与 root 无关的
禁止项**。所以一旦 `code_root` 被设成 `$HOME` 或其上级，
**凭据文件与普通项目文件就完全没有区别**。

## 2. 实测（`probe_write_denylist_gap.py`）

| 用例 | `code_root` | 结果 |
|---|---|---|
| 项目内写普通文件 | 项目目录 | 放行（预期） |
| 项目内写 `.env` | 项目目录 | **放行** |
| 项目内写 `.ssh/id_rsa` | 项目目录 | **放行** |
| HOME 内写 `.ssh/id_rsa` | `$HOME` | **放行** |
| HOME 为 root 时写 `.aws/credentials` | `$HOME` | **放行** |
| 上级目录写 `.netrc` | HOME 的上级 | **放行** |

**结论：`code_root=$HOME` 时能写 `~/.ssh/id_rsa`（能）。**

## 3. 与 Hermes 的差距（两处，第二处我们完全没有）

### 差距一：safe-root 之上没有地板

Hermes 明写：

> **Sensitive paths inside the safe root are still blocked** — pointing
> `HERMES_WRITE_SAFE_ROOT` at `$HOME` does not allow writing `~/.ssh/id_rsa`.

我们没有这一层：`code_root` 就是全部。

### 差距二：**没有「写可、读不回」的凭据保护**

Hermes 对项目内的 `.env` 采取：

> Project-local `.env`, `.env.local`, `.env.production` and `.envrc` files are
> **read-denied anywhere on disk** ... but remain **writable**: the agent can
> create or edit them for you, it just **cannot read the values back**.

**核实我们：`tools.py` 里 `.env` 只出现 1 次**，在 `game_release_check` 的
description 里（「配置、翻译和敏感 .env 文件」）—— **不是保护，是功能描述**。
那 2 处 `BLOCKED` 是 `_BLOCKED_CMDS`（shell 命令黑名单），与凭据无关。

**所以我们项目内的 `.env` 是可读可写的** —— 而那正是最常见的凭据存放处。

## 4. 为什么这一项在调研里被判为「真缺口」

`docs/harness-research-verified.md` §4.2 ③ 列了它，而 ①② 都被判为「暂不做」
（① 没有第二个实现、② 目标已不存在）。**这一项是剩下唯一有实测敞口的。**

## 5. 一处**看起来有防护、其实是副作用**的地方

实测五类路径**全部被 `_resolve_in_root` 拒掉**：

```
\??\UNC\evil\share      -> 拒绝
\\.\PhysicalDrive0      -> 拒绝
\\?\GLOBALROOT\Device   -> 拒绝
\\?\C:\ok.txt           -> 拒绝     <- 合法路径，Hermes 说不该拒
\\server\share\x        -> 拒绝     <- 合法路径，Hermes 说不该拒
```

**但那不是防护。** `_resolve_in_root` 的判据是：

```python
target = os.path.normpath(path if os.path.isabs(path) else ...)
if not (target == root_abs or target.startswith(root_abs + os.sep)):
    return None, root_abs
```

Windows 上**绝对路径必须带盘符**，而 `\??\...` / `\\.\...` / `\\server\...` 都不满足
`root_abs` 前缀，于是被「越界」**顺带**拒了。

**这与 Hermes 的做法有实质差别：**

| | Hermes | 我们 |
|---|---|---|
| 识别依据 | **前缀显式列出**（`\??\` `\\.\` `\\?\UNC\` `\\?\GLOBALROOT` 四类） | 「不像本机路径」—— **副作用**，不是识别 |
| 合法 UNC / 扩展长度路径 | **不拦**（`\\?\C:\ok.txt`、`\\server\share` 明确不受影响） | 顺带拦了（**过度拒绝**） |
| `code_root` 形态变化时 | 那层判据不变 | 我曾断言「UNC root 下会失效」—— **实测不成立** |

⚠ **更正（三次，一次比一次弱）**：

1. 我先写「`code_root` 若是 UNC 或换盘符，判据就失效」
   → **实测不成立**：用 `os.path.normpath` 复现那段判据，喂
   `code_root=\\server\share\proj` 配 `\??\UNC\evil\share`，**仍然拒绝**
2. 我改写「我们先 normpath 再判，所以**解析这个动作本身仍可能发生**」
   → **实测也不成立**。`_resolve_in_root` 第二段确实有 `os.path.isfile(alt)`，
   但喂真实 `code_root` 实测：三类设备命名空间路径全部 **0.000 秒**返回拒绝 ——
   **前缀判据先命中，`isfile` 根本没被走到**。
3. 顺带确认「解析会泄漏 NTLM」指的是 `open()`：直接 `open(r'\??\UNC\...')`
   耗时 **2.698 秒**（真去尝试 SMB 认证，域不存在所以失败）。
   `os.path.normpath` 本身是纯字符串规范化，0.0000 秒，不碰文件系统。

**所以这条的真实差距只剩一条，而且比我最初写的小得多：**

| | Hermes | 我们 |
|---|---|---|
| 拒绝依据 | 前缀**显式列出** | 前缀**碰巧**不匹配本机 root —— **副作用，非识别** |
| 合法 `\\?\C:\` / `\\server\share` | **不拦** | **顺带拦了（过度拒绝）** |
| 设备命名空间路径 | 字符串级拒绝，任何平台 | 目前**也拦住了**，但理由是错的 |

**结论：这不是安全敞口，是一个「靠巧合成立」的护栏 + 一处过度拒绝。**
理由错比没有更危险 —— 下一个人照着现在的代码推断，
会以为「NT 设备路径已被专门防护」，而实际上换个 root 形态或
Windows 行为变化都可能让它失效。Hermes 那种「显式列出 + 任何平台」
的做法才是**不依赖巧合**的。

**但优先级因此下调**：这不是「能写进 `~/.ssh`」那类敞口，
是「护栏意图没有被代码表达」那类债。**真敞口仍是 §2 那条** ——
`code_root=$HOME` 时凭据路径无差别。

## 6. 判据不能拍脑袋 —— 硬拦哪些路径

**优先级说明**：本篇开头把这一项列为「真缺口」，但 §5 三次更正之后
**真实敞口只有 §2 那条**（`code_root=$HOME` 时凭据路径无差别）。
§5 那个「靠巧合成立」的护栏是**设计债**，不是安全敞口。

Hermes 的清单分两类，这个划分值得照抄：

| 类别 | 例子 | 处理 |
|---|---|---|
| **凭据存储**（任何 root 下都禁） | `~/.ssh/`、`~/.aws/`、`~/.kube/`、`/etc/sudoers`、`~/.netrc`、各工具的 token 目录 | **硬拦，无 override** |
| **控制文件** | Hermes 的 `auth.json` / `config.yaml` | **读拒绝、写允许** |

我们要定的三件事，都得先答：

1. **哪些路径硬拦** —— 上表第一类，但要注意 Windows 差异
2. **读是否也拦** —— Hermes 对 `.env` 是「写可读禁」，我们目前读写都可
3. **能不能 override** —— Hermes 的硬地板**没有** override flag

**Windows 特有**（Hermes 也有，但我们必须自己处理）：`\\?\` / `\\.\` /
`\??\C:\` 这类 NT 设备命名空间路径 —— Hermes 明确说「merely resolving such a
path triggers outbound SMB authentication and can leak the user's NTLM hash」。

## 7. 实现前必须先答的问题（本篇不定）

这一项要改**工具落盘检查**，属生产代码，且 24 个写工具的落盘路径**不统一**
（`tools.py` 里 `code_root` 出现 86 次，`_resolve_in_root` / `_path_within_real_root` /
`_resolve_external_path` / `_resolve_create_path` / `_resolve_region_path` 各管一段）。

**所以先要回答：地板加在哪一层？**

| 选项 | 说明 | 代价 |
|---|---|---|
| **A. 加在 `_resolve_in_root` 等入口函数里** | 改 5 个函数，每处加一次检查 | 容易漏（新增函数就绕过） |
| **B. 加在 `execute_tool` 的统一出口** | 一处收口 | 可能覆盖不到不走工具执行器的路径 |
| **C. 先做只读的「探针 + 立档」，不动生产代码** | 把敞口量化清楚，留给下一笔 | 本轮不做修复 |

**我倾向 A+B 组合**：入口函数做主要拦截，`execute_tool` 出口做兜底 ——
因为 Hermes 的 hardline blocklist 是在**审批层之前**生效的，语义上更接近「统一出口」。

**这一项需要单独立项**，不在调研收尾里顺手做。

---

## 8. 实现落地（2026-10-14）

已按 A 方案落地（本仓库没有 `execute_tool` 统一出口，24 个写工具全部经 5 个
`_resolve_*` 入口函数收口，故入口函数拦截即为「统一收口」；B 的兜底诉求已被 A 覆盖）。

### 8.1 新增与 root 无关的地板（`tools.py`）

- 新增纯函数（位于 `_path_within_real_root` 之后）：
  - `_hard_floor_reason(target)` → 返回拒绝原因或 `None`；覆盖凭据存储类 + NT 设备命名空间。
  - `_nt_device_prefix(p)` → 显式识别 NT 设备命名空间前缀（`\??\` `\\.\` `\\?\GLOBALROOT` `\\?\UNC`），
    **消除 §5「靠巧合成立」的护栏**——不再依赖「前缀不匹配本机 root」的副作用。
  - `_is_credential_store(target, low)` → 凭据存储类按路径分量任意位置命中（不只 HOME）：
    `.ssh/.aws/.kube/.gnupg` 任一上级目录、`basename == .netrc`、`/etc/sudoers`、
    Windows `APPDATA` 下的凭据/令牌子目录。
  - `_env_read_denied(target)` → `.env`/`.env.*`/`.envrc` 读被拒、写仍允许（§3 差距二）。
- 接线点：
  - `_resolve_in_root` / `_resolve_create_path`：返回形态由 `(target, root_abs)` 改为
    `(target, root_abs, reason)`，地板拒绝时 `reason` 为人类可读安全文案（调用方优先用 reason，
    避免把「凭据地板拦截」误报成「不在根目录内」）。
  - `_resolve_region_path` / `_resolve_external_path`：保留 `(target, reason)` 形态，地板拒绝时返回原因。
  - `read_file` / `inspect_data_file`：解析后先查地板 + `.env` 读拒。
  - `data_formats._safe_output`（写数据文件的出口）：延迟 `from tools import _hard_floor_reason`，
    避免与 `tools` 的循环依赖，使 `generate_data_file` 也受地板约束。

### 8.2 三件待答事项的最终裁定

| 待答 | 裁定 | 依据 |
|---|---|---|
| 硬拦哪些路径 | 凭据存储类（任意位置）+ NT 设备命名空间 | Hermes hardline blocklist，无 override |
| 读是否也拦 | 凭据存储类**读写都拦**；`.env` 类**写可读禁** | 私钥/令牌不该被读也不该被写；`.env` 仅写可读禁 |
| 能否 override | **不能**（无 flag） | Hermes 硬地板无 override |

### 8.3 验收（可复算）

- 探针 `tools/mutation/probe_write_denylist_gap.py` 现输出「**没有敞口**」：
  `code_root=HOME` 写 `~/.ssh/id_rsa` → 拒绝；上级目录写 `.netrc` → 拒绝（§2 全部行关闭）。
- 新增单测 `tests/test_write_path_floor.py`（7 例全过）：凭据任意位置被拦、普通路径放行、
  NT 前缀显式识别且合法 UNC（`\\server\share`）不被误伤、`_resolve_in_root` 不论 root 如何都拦凭据。
- 回归：相关文件工具测试模块（test_agent_code_tools / test_read_file_* / test_data_formats /
  test_write_* / test_file_conflict / test_subagent_write_auth / test_agent 等）134 例通过；
  唯一失败 `test_planner_cannot_widen_tools_or_enable_mcp` 经 `git stash` 验证为**改动前既有失败**，
  与本次无关。

### 8.4 已知残留（非安全敞口，记一笔）

- 合法 UNC（`\\server\share`）仍被 root 越界判定顺带拒（§5 已记：Hermes 不拦合法 UNC）。
  这是「过度拒绝」而非敞口；本次未动，因不在 §2 敞口范围内，且改它需权衡 Windows 行为。
- 项目内恰好含同名目录（如 `.ssh`/`.aws`）时也会被拦——属有意的强化，已在探针与单测中固化。