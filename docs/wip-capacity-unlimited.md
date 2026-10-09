# WIP 归档：容量默认放开为「不限」（max_nudges 等）

> **状态：写了一半，13 小时后无人认领。** 本文档把它变成可被任何人接走的清单。
> **代码状态：未提交，全部在工作树里。**

## 这批活是什么

把六个容量字段的**默认值从具体数字放开为 0（= 不限）**，让长任务不再被
粗粒度上限掐断；同时把两个上界从 20 提到 1000。

已改（后端，未提交）：

| 文件 | 改动 |
|---|---|
| `agent_runtime/capacity.py` | `DEFAULTS` 六项全改 0；`max_nudges` / `max_final_continuations` 上界 20 → 1000 |
| `api.py` | 纠偏/续写默认同步放开为 0 |
| `agent.py` | 接入 `_capacity_settings()`，按运行时上限判是否还能纠偏 |
| `config.py` | `OUTPUT_TOKEN_RESERVE` 常量提出；`deepseek-flash` 的 1M 上下文覆盖 |

## ⚠ 还没做的

### ① 越界 / 负值处理 —— ✅ 已修（D 方案，用户拍板）

原问题：`normalize_settings` 越界时回落 `DEFAULTS`，而 WIP 把默认全改成 0
（= 不限），于是**用户填 5000 → 静默变成不限**。想收紧的人拿到最宽松的值。

**分层契约（2026-10-09 定）：**

| 输入 | 处理 | 理由 |
|---|---|---|
| 超过上界（5000） | **夹到上界**（1000） | 「想设很多」是合理意图；语义仍是收紧 |
| **负值（-1）** | **raise ValueError** | 无法推断意图；夹到 0 = 不限，是安全方向的反面 |
| `0` | 原样保留 | 合法的不限 |
| 非数字 / `None` | 回落 DEFAULTS | 无法解析，不是用户明确表达的值 |

**raise 的三个调用方，处理不同：**

| 调用方 | 处理 | 为什么 |
|---|---|---|
| `api.set_agent_capacity` | `try/except` → **400** | 请求边界，raise 的预期用途 |
| `config._apply_persisted_state` | `try/except` → DEFAULTS | **开机路径**，坏状态不能让服务起不来 |
| `capacity.get_settings()` | 已有 `try/except` | 同上（本来就有，未改） |

**顺带修掉的前置校验**：`api.set_agent_capacity` 原本用 `0 <= x <= 1000`
把 5000 判成 400，与「夹到上界」矛盾。已改为**只拒负值**。上界的唯一权威
实现在 `capacity.py` 的 `_clamp`，不在这里重复 —— 重复就会漂移，而漂移的
方向正好是「界面放行、后端拒绝」。

⚠ **一个必须记住的副作用**：`api.py:4225` 原注释写着「normalize_settings
只拒绝 <0」，D 之后**变成假的**，已同步改。改契约时改了行为不改注释，
下一个人会照着错的注释写代码。

**改过的三条既有测试**（改前提，未删）：

| 测试 | 原断言 | 新断言 |
|---|---|---|
| `test_an_out_of_range_value_is_still_clamped` | 99999 → 默认 | → 1000（方法名一直叫 clamped，旧实现做的却是 fall back，名实不符） |
| `test_out_of_range_nudges_fall_back_to_unlimited` | 99999 → 0 | → 1000（作者有意让越界变不限；保留其顾虑「别回落成正数」，修正结论为夹取） |
| `test_a_negative_value_is_still_rejected` | -1 → 0 | → raise（名字说 rejected，做的是 clamp 到 0 = 不限，正是要堵的坑） |
| `test_invalid_limits_fall_back_to_safe_defaults` | 混判两种语义 | **拆成三条**（上界夹取 / 负值 raise / 非数字回落） |

**变异实测**：上界不夹、负值不 raise、0 被当越界 —— 三种改坏各自被对应
断言抓住（全部变红后还原）。

### ② 六个兜底魔数 —— 已修（`6fc0c66`）

`SettingsView.vue` 的 `confirmUnlimitedXxx` 里 `?? 2` / `?? 8` / `?? 24` /
`?? 50000` 全部过期（后端默认已改 0）。已改为不写数字：

```js
String(capacityInfo.value?.settings.max_nudges ?? '')
```

锁测试 `tests/test_capacity_bounds_ui_backend_agree.py` 两条断言
（上界相等 / 兜底值不写死魔数）都已落地并做过变异实测。

**移交说明**：这六处本来属于前端 agent 的文件，由其明确授权后由后端改。

### ③ 六个 `window.confirm` 每字段弹一次 —— UX 缺陷

`SettingsView.vue` 六个输入框各挂 `@change="confirmUnlimitedXxx"`，
默认全是 0，**切一遍 tab 能被弹六次**，保存时还有一次。

该退化成「标记待确认」，不在 blur 级弹原生框 —— 顺带避开 pywebview 里
原生模态阻塞 JS 的问题。保存时的那个合并确认已存在且是对的。

## ④ 边界数字抄了两份（可选，下一步）

前端 input 的 `min/max` 与后端 `normalize_settings` 各写一套，今天靠
`test_capacity_bounds_ui_backend_agree.py` 锁住相等。

彻底解法是让后端 GET 载荷下发范围（`api.py` 加一个字段 + 前端消费），
单源真值。当前先用锁测试兜住，因为「没人知道会漂移」比「漂移本身」更疼。

## 关联影响：问答页像素基线

侧栏模型名 `qwen3.6:35b-a3b-agent25 + 6k → agent256k` 来自这批未提交的
`config.py` / `capacity.py`。**基线要等这批落定后再 `--update`**，
否则会把中间态烧进基线。

## 建议顺序

1. ~~先修 ①（越界夹取）~~ —— **已完成**（D 方案，含分层 raise + 前置校验修正）
2. **提交整批 WIP**（含 ①② 与四条改过的测试）—— 待办
3. ③ 六个 `window.confirm` 合成一个 —— 动 `SettingsView.vue` 结构，待前端 agent
4. 问答页像素基线更新放最后

## 相关

* `tests/test_capacity_bounds_ui_backend_agree.py` —— 边界/默认值锁（`6fc0c66`）
* `docs/next-steps.md` §7 —— 容量相关的待办索引
