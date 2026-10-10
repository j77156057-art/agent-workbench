# 测量纪律：跑测期间不改被测文件

## 实测事故（2026-10-10）

一轮全量报 **27 failed**。看起来像新缺陷。

实际上**那 27 条没有一条是真缺陷** —— 全是**我在那轮跑的过程中**改写了
被测文件 `agent.py` 造成的。

## 机制

`tests/test_channel_protocol_consistency.py` 等 26 条用源码扫描判据：

```python
src = inspect.getsource(agent_mod.Agent._run)
assert "format_stalls" in src
```

`inspect.getsource` 靠 **`co_firstlineno`** 定位 —— 那是**导入那一刻烧进
字节码**的行号。文件之后再怎么改，这个数都不会更新。

我在 `run()` 里插了 13 行 -> 行号全部移位 -> `getsource` 从错位的行号
切出一段**别的函数体**。实际失败长这样：

```
E   assert 'format_stalls' in '                try:\n'
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                   getsource 返回的全部内容，17 个字符
```

复现（用一次性小模块，不碰 agent.py）：在文件前插入 13 行后，
`getsource(Thing._run)` 返回的是 `helper` 函数的源码，截断在 `try:` 行 ——
与实际失败逐字同型。

## 27 条的签名分布（全部同型）

```
x5  AssertionError: 查错了函数：_run 里没有 format_stalls
x5  ValueError: substring not found
x11 'X' not found in '            self.last_turn_record = rec ...'
x1  'yield' not found in '\n'
x4  AssertionError: -1 == -1 : 停滞分支不在了 ...
x1  '...projB' != '...projA'     <- 这条是另一回事（新语义冲突，已改判据）
```

全是「要找的东西不在一段错位切出来的源码里」。

## 与前一晚事故的对比

同一晚，两个报告：

| 报告 | 缺陷 | 处理 |
|---|---|---|
| 93 failed | **真的** —— contextvar 无 reset 点 | 改了生产代码 |
| 27 failed | **假的** —— 我自己改被测文件 | 不用改任何代码 |

**差别只有一处**：93 那轮我全程没碰 `agent.py`；27 那轮我碰了两次
（`fix_scope_4.py` 落盘、`compare_states.py` 改写）。

## 纪律

### 1. 全量回归启动后，工作树冻结

唯一允许的写操作：**往 `D:\Temp\opencode\` 写报告**。

要改代码 -> 先等它跑完。改完再跑一轮。

### 2. 需要并行验证时，用只读副本

把工作树 `git archive` 或 `robocopy` 一份到 `D:\Temp\opencode\wt\`，
在那份上改。那份的 `agent.py` 与被测文件同步变化，不会移位。

### 3. 变异实测期间尤其危险

变异脚本本来就是「**改坏生产代码**」。它与全量并行 = 灾难组合。
**变异实测必须独占工作树。**

### 4. 报告里的失败，先分「真缺陷」和「我的操作」

判据：**失败信息里出现的东西，能不能在当前代码里手动找到？**

- 找到 -> 真缺陷
- 找不到，但代码明明有 -> 先怀疑自己动过被测文件

### 5. `inspect.getsource` 型测试的自查

这类测试有个固有脆点：它**对文件行号敏感**。任何
「改文件 + 不重新导入」的并行操作都会让它失效。

跑这类测试前先确认：`git status` 干净，且本轮没改过被测文件。

---

## 与其它纪律的关系

同一病根的其它三张脸：

| 纪律 | 病根 |
|---|---|
| `verifier-discipline.md` | **没有覆盖的判据**不能算通过 |
| `transform-script-false-success.md` | **没有落盘的改动**不能算改过 |
| 本文 | **没有被保护的环境**不能算测量 |

都是「**没被验证的那一步被当成验证过了**」。
