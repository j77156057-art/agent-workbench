# 缺陷立档：叙述开关开着，但这个模型不写正文散文

> 结论一句话：**提示词这条路对这个模型无效。真正的内容在 reasoning 里，
> 而 DocMind 把 reasoning 默认折叠。**

## 状态

| | |
|---|---|
| 立档 | 2026-10-08 |
| 相关提交 | `18cf7bd`（开关本体）、`7f9d246`（修并行批次路径） |
| 判定强度 | **n=1**（同一题复跑 + 换题复跑在补，见下） |
| 影响面 | 只影响「模型在工具调用之间说话」这一条体验；**不造成功能损坏** |

## 实测经过

### 第 1 次：功能根本没被调用（这是个 bug，已修）

`tools/probe_narration_supply.py` 报：

```
*** `offer` 一次都没被调用 —— **本次不产出任何结论**
     （生产代码可能已不走这条路径；「没抓到」不等于「模型没写」）
```

**恒真防护在这里救了整件事。** 如果探针当时编个理由（比如「模型没写散文」），
后面所有方向都会错。

查下去发现是**守卫漏了一条路径**（`agent.py:4841`）：

```python
if len(pairs) > 1:
    self._pending_batch = pairs      # 批次分支：不设 native_override
else:
    native_override = (_nm, _nin)
    acc = ... + f"Action: {_nm}\nAction Input: {_nin}"
```

批次分支的 `acc` 里**没有合成的 `Action:`** → `parsed["action"]` 恒空 →
守卫 `elif self.narration and parsed["action"]:` **在最常见的路径上永远是假的**。

不报错、不掉事件，只是「一直没解说」。`agent.py:5596` 的注释里其实早就写着
「`len(pairs) > 1` 那支只设 `_pending_batch`、不设 `native_override`」——
**代码库知道这个分支，只有我不知道。**

⚠ **为什么单测测不到**：当时的假 LLM 只发单调用。真实模型一次发 3 个
`read_file`，走批次路径。**测试用的是我脑子里模型的行为，不是真实模型的行为。**

修法：守卫改成「这条消息派发了工具」，覆盖三条路径
（`parsed["action"]` / `native_override is not None` / `self._pending_batch`）。
三条里**没有一条是最终答案**，所以「答案漏进时间线」那条防护仍然承重。

### 第 2 次：修复被真跑证实，但暴露出供给侧是空的

同一题、同一模型、同一 fixture：

```
事件: 总 19  {'reasoning': 9, 'action': 3, 'observation': 3, 'token': 2, ...}

=== offer 被调用 1 次 ===
  [1] acc 0 字 -> 剥后 0 字 -> 放行 0 字
      理由: acc 为空 —— 模型这一轮没写任何正文

=== 判定 ===
  📌 **(b) 模型这一轮没写任何正文散文**
```

**`offer` 从 0 次调用变成 1 次调用** —— 守卫修好了。
**但 `acc` 是 0 字** —— 模型在工具轮里**一个字都没写**。

## 两种配置都拿不到可用叙述

| 配置 | 正文散文形态 | 结论 |
|---|---|---|
| **thinking 开** | **空**（内容全在 reasoning） | 提示词无效 |
| **thinking 关** | 有，但是 `Thought:` **协议形态** | 契约压制住了，没法直接用 |

thinking 关闭时实测 `thought = 2`（`"Thought: 用户问的是…"`），
而契约第 3 条明文禁止这个形态 —— **提示词说了不算，模型照样写**。

所以两条路都不通：
* 让模型写正文 → 它不写（thinking 开）
* 强制它写 → 它写协议文本，而协议文本不是人话

## 那真正的杠杆在哪

**reasoning 里已经有大量面向用户的话。** 实测样本：

```
list_dir 或者 search_code 应该都能找到。
为了稳妥，我先用 list_dir 看看根目录下有没有这些文件
让我先调用 read_file 读取这三个文件。
```

**「让我先调用 read_file」就是用户想听的「边干边说话」** —— 它存在，
只是躺在 reasoning 通道里，而那个面板：

```tsx
{{ reasonOpen.has(m.id) ? '▾ 深度思考' : '▸ 深度思考（点击展开）' }}
```

**默认折叠。**

对照三家（`docs/harness-research-verified.md`）：
* **Hermes**：`show_reasoning: true` 默认，**实时流式**
* **opencode**：`session-ui` 默认显示（`showReasoningSummaries` 默认 true）
* **DocMind**：**默认折叠**

**所以 DocMind 是三家里唯一把 reasoning 藏起来的。**

## ⚠ 这条立档不能被当成「叙述功能无用」

必须说清三件事：

1. **功能本身是通的** —— 守卫修好后真跑证实 `offer` 被调用；
   剥协议、同轮去重、不污染答案三条防护都有单测 + 真跑。
2. **关掉时零行为变化** —— `narration=False` 与改动前逐字一致（有测试钉住），
   所以这个开关**不会造成任何退化**，只是对当前模型**暂时没有收益**。
3. **换模型可能有收益** —— 如果某个模型愿意在 tool_call 旁边写散文，
   这个开关立刻就生效。**不能因为一个模型不写就断定这条路不通。**

## 什么证据能推翻本条

* 换 `DOCMIND_E2E_MODEL` 跑 `probe_narration_supply.py`，
  若某模型 `acc` 非空且放行 → 「对所有模型无效」这句话被推翻
* thinking 关时若模型能写出**不带 `Thought:` 标记**的散文 → 契约可被压住

## 待补

* n 从 1 提到 3（同一题复跑 + 换题复跑）
* 其他 provider（deepseek / qwen 云端）的供给侧读数

## 相关

* `tools/probe_narration_supply.py` —— 供给侧探针（含恒真防护）
* `tools/probe_narration_event_mix.py` —— 事件构成探针
* `docs/research-discipline.md` 第 4 条 —— 「字面为真 ≠ 功能性结论」
