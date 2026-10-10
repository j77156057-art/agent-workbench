# 已知缺陷：`code_root` 是全局/项目级，不是任务级 —— 换任务后检索器在旧代码里翻

> **发现于** 2026-10-10，一次 5 个子任务 0/5 全败的排查。
> **性质**：设计缺陷（不是某次崩溃）。它让「换了任务、代码没换」，
> 检索器于是在**上一个任务留下的代码**里搜索。
>
> **本文的每条断言都实测过**（方法见 §6）。

## 1. 实测事故

用户在自主开发舱发起一个「优化界面」的工作流，**5 个子任务 0/5 完成**，
重规划 2 次仍全败。`research` 子代理连续三轮都在搜 `mean` ——
而 `mean` 与「优化界面」毫无关系。

排查发现三层事实叠在一起：

| 层 | 事实 |
|---|---|
| ① 任务错配 | research 搜 `mean`，任务却是「优化界面」 |
| ② **代码根指向 mean 题的遗留代码** | 见 §3 —— **本文的根因** |
| ③ 核验把否定句判成引用 | 已修，见 `docs/` 里核验那条 |

**②是本文要记的**：即使 ③ 修好，检索器仍然会在错的目录里搜。

## 2. 根因：`code_root` 的作用域比任务粗

实测本机状态（`.docmind_state.json`）：

```
顶层 code_root                       = D:\Temp\opencode\docmind_remediate_probe
当前 project_id                     = prj-3da07fde89a5
该项目自己的 root                    = D:\WorkBuddy\godot_sample
```

那个顶层 `code_root` 指向的目录里是：

```python
# calc.py
def mean(values):
    """求平均数。"""
    if not values:
        return 0
    return total(values) / len(values)   # 已修好：上一轮忘了除以个数
```

```python
# test_calc.py
def test_mean():
    assert mean([2, 4, 6]) == 4.0
```

**这是上一道 mean 题留下的代码**（`web-5415c67293d24d63b048b7fa` 会话，10-05）。

### 两条链路

```
上次做 mean 题 → 那个探针目录被设成顶层 code_root（全局状态）
     ↓
这次在**同一个项目**里问别的 → project_id 相同 → 共用同一份状态
     ↓
research 在 calc.py 底下搜 mean → 搜的是上一个任务的遗留
```

## 3. ⚠ 更糟的一层：顶层值与项目值**不一致**

上面那两条同时成立：

- 顶层 `code_root` = **mean 题的探针目录**
- 项目 `prj-3da07fde89a5` 自己的 `root` = **`D:\WorkBuddy\godot_sample`**

**顶层的盖住了项目的。** 于是：

- 界面上选的项目是 `godot_sample`
- 实际检索用的目录却是 `D:\Temp\opencode\docmind_remediate_probe`

**用户看到的项目与系统实际检索的代码不是同一个。** 而界面上没有任何地方
显示「当前代码根是 X」（实测：`api.py` / `projects.py` 里**没有**任何
「换根前确认」逻辑）。

## 4. 为什么两个会话「同项目」很常见

会话按 `project_id` 分桶：`.docmind_sessions/<project_id>/<session_id>.json`
（`sessions.py:11`）。实测 `prj-3da07fde89a5` 下有 **38 个** `web-*` 会话 ——
mean 题那次与「优化界面」那次都在里面。

所以「同项目 → 共用 code_root」不是罕见路径，是**默认行为**。

## 5. 修的方向（未实施，等人定）

治标（立刻能做）：

- 把顶层 `code_root` 指回项目自己的 root，或清空让它回落
- 在工作台显眼处显示「当前代码根：<路径>」，让人能发现自己搜错地方

治本：

- **`code_root` 应跟着会话/任务走**，而不是只跟项目走；
  或切任务时明确问一次「当前代码根是 X，是否切换」
- 顶层值与项目值不一致时，**以项目为准**（顶层只作全局默认），
  现在的优先级是反的

## 6. 本文怎么验的

```python
state = json.load(open(".docmind_state.json", encoding="utf-8"))
state["code_root"]                      # 顶层值 -> mean 题的探针目录
state["projects"][state["current_project_id"]]["root"]   # 项目值 -> godot_sample
```

两处不一致，且 `calc.py` 里确有 `def mean(values)`，由此定位。

## 7. 与其它两条的关系

| 现象 | 归属 |
|---|---|
| research 搜 mean（任务错配） | **本文** —— code_root 指向旧代码 |
| 核验把「xxx.gd 不存在」判成引用，3 轮全被拒 | 已修（核验识别否定语境） |
| 那 5 句写意图被拦 | 另一族，见 `docs/write-intent-corpus.md` D 节 |

**即使后两条都修好，第一条仍会让检索器在错的目录里跑。**
所以本文单列，不混进那两笔。

## 8. 排查这件事时我自己栽的两次

1. **先断言「失败的工具轨迹被算进引用」** —— 真跑否掉了它，真因在结论文本。
2. **说「那个 Temp 目录是我今晚建的」** —— 错，是 10-05 的。
   凭名字里的 `probe` 就认定是自己的，没查 `CreationTime`。

两处都靠真跑/实查纠正，没有靠推理硬下结论。
