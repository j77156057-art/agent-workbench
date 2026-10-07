# 设计：用一次「用户批准」铸造 capability lease

> 状态：设计已定，**未实施**。
> 目标缺陷：多轮批准失效（`改吧`/`动手吧`/`写吧` 等被拒），已实测确证。

## 1. 要解决的问题

写意图分类是**逐条消息、无状态**的纯函数 `_write_intent(question)`。实测：

```
denied : 改吧 动手吧 那就修吧 写吧 开始改 就这么改 你改吧 修吧 改 修
granted: 动手改 改一下 动手修
```

于是「用户已批准 → agent 仍不动」的死循环。词表里全是多字形（`动手改`/`改一下`/
`改一改`/`帮我改`/`只改`），唯独没有裸 `改`/`修`/`动`/`写`。

**不能靠扩词表解决**：`改吧` 与「这个方案怎么改比较好？」在词形上无法区分。

## 2. 关键发现：结构化授权的机制**已经存在**，只是从不启动

`agent.py:3063` `_lease_blocked(action_name)`：

```python
spec  = self._tool_spec(action_name)      # 工具自己声明的 capability
lease = self.capability_lease
if not lease or spec is None: return False, ""     # ← 没有租约就永远放行
if status != "active":                  return True, "已释放或无效"
if expires_at <= time.time():           return True, "已过期"
if capability not in lease.capabilities: return True, "未租用该能力"
```

这套判定的依据是**工具自己的 `spec.capability`**，不是用户那句话 —— 正是
`codemode` 那套「权威来自结构而非意图」的形态。而且：

- 有 **TTL**（opencode 的权限表没有过期列，我们自己有）
- 有 **release** 状态
- **父子继承**：`agent.py:5430` `capability_lease=self.capability_lease`
- 有 3 条测试：`tests/test_capability_lease.py`

### 但两端是断的

| | 消费点 |
|---|---|
| `_lease_blocked`（检查侧） | ✅ 活的：`agent.py:4418`、`agent.py:5146` |
| `create_capability_lease`（铸造侧） | ❌ **无生产调用点** |

`_ensure_capability_lease` 的两个调用点（`game_workflow.py:3698/3872`）都在
**游戏工作流引擎**里，不在对话路径。

**结论：对话场景下 `self.capability_lease` 恒为 `{}`，检查侧永远收到空租约、永远放行。
lease 机制完全休眠。**

## 3. 方案：桥

```
用户「改吧」
  ↓
_write_intent → granted（本轮）
  ↓
铸 capability_lease：capabilities = 本轮写工具的 capability 集合，TTL = 一轮
  ↓
本轮余下时间由 _lease_blocked 做**结构性**判定，不再猜语言
  ↓
下一轮 lease 过期 → 自动回到护栏兜底
```

### 三条设计约束

**1. `_write_intent` 保持纯函数、一行不改。**
会话层是新增的包装（`_write_intent_for_session`）。因此
`tests/test_write_intent_*.py` 那 90+ 条守卫**一条都不用改**。这是本方案
最大的安全网 —— 纯函数的行为被完全冻结，新逻辑只做叠加。

**2. 租约从 `granted` 轮次铸造，不由语言猜测铸。**
铸造者是**判定结果**，不是文本匹配。这样就不必去区分「改吧」和「怎么改」。

**3. 租约能力集从工具声明反推，不硬编码。**
用 `agent_runtime.tools._WRITE_TOOLS` 对应的 `Capability.WRITE_LOCAL/WRITE_EXTERNAL`
—— 即已有的 `_is_write_tool`（`agent.py:3087`）判定逻辑。

## 4. 子 agent（用户提问的核心）

**子 agent 不需要自己的授权，因为它继承父的租约。**

- 子 agent 构造时 `session_id=None`（不落盘），所以**按 session_id 存的方案对它无效**
  —— 这条已排除。
- 但 `capability_lease` 是按对象传递的（5430 行），子 agent 拿到的是**父已经拿到的那份批准**，
  不是空白支票。
- **父有有效租约 → 子能写，且不需要问任何人**（因为批准已经发生过）。
- **父无租约 → 子被拦**。此时 `ASKED` 在子 agent 里是**死状态**（没有用户可问），
  所以子**绝不能进入 ASKED 循环** —— 它的阻塞理由必须作为
  「需要用户确认」**上浮给父**，由父去问用户。

`_run_child`（5234 行）返回 `{status, conclusion, steps, error}`，需要新增一个
「授权上浮」的状态位，让父知道该问用户什么。

## 5. 裸动词这条路：**已实测否决**，别再走

> 本节原写「裸 `改|修` 可安全加、0 误放行、风险已量化、先落这一笔」。
> **那是错的，实测 3 条守卫红。** 下面是修正后的结论。

### 实测：加裸 `改|修` 会破 3 条守卫

临时把 `改|修` 加进 `_WRITE_VERBS`（改完立即还原，未留痕），跑
`test_write_intent_vocabulary` / `_negation` / `_three_valued` / `_question_form`：

```
3 failed, 167 passed, 17 subtests
```

| 被破的守卫 | 破坏了什么 |
|---|---|
| `test_a_sentence_with_only_a_bare_verb_is_not_granted` | `6069386` 留的**裸动词限制记录** |
| `test_the_clarification_prompt_also_respects_a_prohibition` | **否定作用域** —— `e0bdb53` 刚修好的 |
| `test_a_conditional_sentence_with_an_unlisted_verb_falls_back_to_denied` | **条件式 fallback** —— `966212a` 刚修好的 |

后两条**事前完全没料到**：裸 `修` 会把否定词和条件式的作用域一起吃掉。

### 名词陷阱那一档也不成立

`动` / `写` 确实有名词陷阱（`写操作`/`写工具`/`移动端`/`怎么写的`），
已有守卫 `test_bare_verbs_stay_out_of_the_vocabulary` 钉的就是这个。
但既然 `改`/`修` 已被否决，整条「加裸动词」的路都不成立。

### 为什么会误判（同一个会话里第四次）

事前那版探针的判据是 **`_write_intent(...) != DENIED`**，而守卫断言的是
**`_has_write_intent(...) is False`**。`uncertain` 在我的判据下算「没误放行」，
在守卫下算**红** —— 判据不同，结论就不同。

而且只测了 `MUST_STAY_DENIED` 一个文件，没跑 `negation` / `three_valued` /
`question_form`。**两个错误叠加，把 3 条红读成了 0 条。**

同一会话的前三次同类错误：`isinstance(spec, dict)` 判 `ToolSpec`、
`startswith("slowest durations")` 判表头、「数量相等」当「逐项相等」。

### 结构性的理由，比实测更重要

即便没有那 3 条红，这条路也不该走：**词表是逐条消息的，而要修的缺陷是跨轮的。**
往词表里加跨轮批准语（「改吧」），必然要开始处理
「否定 + 批准语」「条件式 + 批准语」的组合 —— 那是无限 regress。

`agent.py:1803-1808` 那段「⚠ 刻意**不加**的」注释是**对的**，不要动它。

## 6. 结论：跳过词表，直接做租约

租约方案**不碰词表** —— 它在**判定结果**上做文章：

- 否定、条件式、疑问句照旧由 `_write_intent` + 词表处理
- 跨轮批准由「granted 轮次铸租约 → `_lease_blocked` 结构性判定」处理
- 两边不冲突，也不互相 regress

**实施顺序（原第 1 笔作废）**：

1. `_mint_write_lease(turn)` —— 只加铸造，不接判定，先让单测能覆盖
2. 在 `granted` 路径接上铸造 —— `_write_intent` 本身不动
3. 子 agent 阻塞理由上浮
4. `ASKED` 状态的肯定/否定封闭二分类

**动手前的前置**：变异测试要打在**四个守卫文件全跑**上，不能只跑
`MUST_STAY_DENIED` —— 这次就是只跑一个文件才漏掉两条红。

## 7. 已知风险

1. **语音转写可能把否定说成肯定**（`HANDOFF.md` 提到 final 收尾会走语音转写）。
   误判方向是**放宽**。缓解：`ASKED` 只在 agent 刚问过时有效，且只认**短肯定句**。
2. **子 agent 上浮的阻塞理由若丢失**，父会以为子失败了而不是「需要确认」。
   缓解：把上浮做成 `_run_child` 返回结构里的**必填字段**，缺了就当失败。
3. **租约铸造点选错会造成误拦**：若在 `uncertain` 时铸，用户问完还是不能写。
   缓解：只在 `granted` 铸。
4. 租约跨轮**不做路径级作用域**（v1）。`只改 b.py` 这类请求自带范围，不受影响。

## 8. 实施顺序（每步独立可提交、独立可验）

> 原第 1 笔「裸 `改`/`修` 进词表」**已作废**，见第 5 节。

1. `_mint_write_lease(turn)` —— 只加铸造，不接判定，先让单测能覆盖
2. 在 `granted` 路径接上铸造 —— `_write_intent` 本身不动
3. 子 agent 阻塞理由上浮（`_run_child` 返回结构加必填字段）
4. `ASKED` 状态的肯定/否定封闭二分类

第 1 步刻意**只加铸造、不接判定**：这样「铸了租约但没人消费」这个中间态是可测的，
而不是等到第 2 步才发现铸造点选错。