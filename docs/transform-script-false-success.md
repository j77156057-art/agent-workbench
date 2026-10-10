# 变换脚本的「假成功」

## 症状

逐步替换锚点、逐步打印成功，最后一步失败退出 —— 但**文件从没落盘**，
而前面每一步都打印了「已 set」「已 reset」。

## 实例（2026-10-10，修会话级作用域）

`fix_scope_3.py` 的结构：

```python
src = src.replace(OLD_SET, NEW_SET, 1)
print("  ① run() 里已 set")          # <- 打印了

src = src.replace(OLD_RESET, NEW_RESET, 1)
print("  ② finally 里已 reset")        # <- 也打印了

OLD_IMP = "    set_ctx_session_code_root as _set_ctx_session_code_root,\n)"
if src.count(OLD_IMP) != 1:
    raise SystemExit("  *** 导入锚点 %d 次 ***" % src.count(OLD_IMP))  # <- 这里退出了
...
A.write_text(src, encoding="utf-8")   # <- 从未执行
```

锚点写错了（import 那行后面还有一行，不是直接 `)`），第三步退出，
**前两条 print 是彻头彻尾的假成功**。

更糟的是它**看起来像成功了**：我接着跑最小复现脚本，
看到「无泄漏」就以为修好了 —— 其实那只是因为 `__init__` 不再设值，
而 `run()` 也从来没设过。**整个机制是死的，但两次「验证」都显示正常。**

## 为什么难发现

1. **打印成功 ≠ 落盘成功**。中间隔着一次 `write_text`。
2. **验证脚本验证的是别的现象**。复现脚本测「泄漏还在吗」，
   而泄漏消失的原因恰好也是「功能整个消失」。
   —— **两个不同的失败模式产生了相同的观测**。
3. 绿是**弱信号**，尤其是「本该红」的测试变绿/保持绿的时候。

## 纪律

### 1. 全量校验通过才允许写盘

```python
EDITS = [(label, old, new), ...]

for label, old, _ in EDITS:          # 先全查
    if src.count(old) != 1:
        raise SystemExit(...)         # 一个不过就退出，什么都不写

for _l, old, new in EDITS:           # 全过了才动手
    src = src.replace(old, new, 1)

ast.parse(src)                        # 语法也要先过
A.write_text(src, encoding="utf-8")
```

### 2. 写盘后自检调用点真的在

```python
for label, needle in checks:
    if needle not in after:
        raise SystemExit("  *** 自检失败 ***")
```

不靠「我记得改了」，靠文件里**实际有那行**。

### 3. 变异实测是唯一能抓住这个的关卡

M1「run() 不再 set」→ 判据全红。
如果判据是绿的，那说明它根本没执行到那条路径 —— 和这次的假成功同型。
**改完之后必须跑变异，且「功能删除」型变异必须红**，否则不许声称修好。

### 4. 「绿」要看它是怎么绿的

| 观测 | 可能含义 |
|---|---|
| 目标判据绿 | 修好了 ✅ |
| 目标判据绿，但**另一条**判据也变了行为 | 功能整个消失 ⚠ |
| 泄漏类复现脚本「正常」 | 可能只是功能被删了 ⚠ |

第 2、3 行就是这次踩的。**看到一个绿，先问：它是因为我修好了，还是因为我把功能删了？**

---

## 与「零覆盖判据」的关系

同一病根的另一面：

- 零覆盖判据：**没有覆盖的判据不能算通过**
- 假成功：**没有落盘的改动不能算改过**

都是「**没验证的那一步被当成验证过了**」。
