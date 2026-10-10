# 已知缺陷：`.docmind/` 下的仓库副本与本体并存，grep 会先撞到影子

> **发现于** 2026-10-10 晚。**性质**：排查陷阱（不是功能缺陷）——
> 它不会让程序出错，但会让**人得出基于过期副本的结论**。

## 现象

`.docmind/projects/<项目>/<checkpoint 名>/` 下会存在**整个仓库的完整副本**，
含 `agent.py` / `api.py` / `tests/` / `frontend/` / `docs/` 全套。

第一次量到的实例：

```
.docmind/projects/prj-af6142eae52c/headcheck-10c0d022-84a778/
    856 个文件，含 agent.py、api.py、tools/、tests/、frontend/、docs/…
    其中 docs/write-intent-corpus.md 17.4 KB
```

**而本体那份是 17.0 KB。** 两者内容不同 —— 副本是某个时刻的快照，
可能比本体旧，也可能带着本体已删的东西。

## 为什么危险

**任何全仓 grep / glob 都可能先撞到副本。**

我当晚的实际经过：

1. `Select-String -Path *.py, agent_runtime\*.py, …` 没命中
   → 因为副本在 `.docmind/` 下，glob 没覆盖到（这一步是安全的）
2. 后来 `Get-ChildItem -Recurse -Include "*intent*"` **命中了副本**
   → 读到的 `write-intent-corpus.md` 是 17.4 KB 的那份
3. 如果继续在副本上比对/推理，**整晚的结论都会建立在一份可能过期的快照上**

同类风险：副本里的 `tests/test_write_intent_*.py` 六个文件
与本体**全部哈希不一致** —— 副本的测试是旧版。

## 放大因素

- `.gitignore:64` 忽略整个 `.docmind/`，所以副本**永远不进版本控制**，
  也就没有「副本过期了会有人发现」这条防线
- 副本由 reaper 定期回收 —— 但**回收时机不确定**，同一份文件
  今天在、明天可能就没了（我第一次查到 856 文件，半小时后查是 0 个）

## 判定规则（下次照这个做）

### 搜索时

| 目的 | 该搜哪里 |
|---|---|
| 找**本体**的代码/文档 | 仓库根，或显式排除 `.docmind/` |
| 找**运行时产物**（用户数据、trace） | `.docmind/` 才是对的 |

具体做法：

```powershell
# 对：glob 显式列出本体的目录
#     （实测：搜「项目长期记忆」命中 7 条，含 agent_memory.py 与 docs/）
Select-String -Path *.py, agent_runtime\*.py, agent_memory.py, tests\*.py, docs\*.md -Pattern ...

# 也要当心：glob 写窄了会**漏掉本体里真实存在的答案**
#     （实测：`-Path *.py, agent_runtime\*.py` 只命中 2 条，漏了 docs/ 里的）
#     —— 这不是撞上副本，是另一个方向的错

# 用 -Recurse 时必须排除副本
#     （实测：排除后 451 个 .md；不排除会多出 .docmind/ 与 .venv/ 的）
Get-ChildItem -Recurse -Include *.md |
    Where-Object { $_.FullName -notmatch '\\\.docmind\\|node_modules|\.git\\|\.venv\\' }
```

⚠ **两类错都要防**：撞上副本（读到过期内容），
以及 glob 写太窄（漏掉本体里的答案）。后者我在写这份文档时当场犯了一次 ——
第一版「错例」注释里写「命中 0 是对的」，实测其实是 2 条，说明它**漏了** `docs/` 里的命中。

### 读到疑似副本的东西时

**先比一次哈希，再决定信哪份**：

```powershell
(Get-FileHash docs\write-intent-corpus.md -Algorithm MD5).Hash
(Get-FileHash .docmind\projects\<proj>\<ckpt>\docs\write-intent-corpus.md -Algorithm MD5).Hash
```

不一致就**以本体为准**，并顺手确认副本是不是更旧
（比较 `LastWriteTime`，或看它是否含本体已删的文件）。

### 判断自己是否踩了

一句话自查：**我引用的那个文件，在本体里存在吗？**

- 存在 → 没问题（但要确认内容一致）
- 不存在 → **我读的是运行时产物，可能是过期的**，结论要重做

## 当晚的实际代价

我在这个问题上花了一整晚（找 `写/实现/设计` 那 5 句的修法），
而 `docs/write-intent-corpus.md` 的 D 节标题就是
「含取消/回滚/终止字的正常请求被拦（未修）」——
**答案早就在本体里**，且 `docs/next-steps.md:195` 与 `:278` 引用了它两处。

不是「位置没记」，是**没查 + 先撞到了影子**。

## 附：`.docmind/` 里哪些是真数据

不是所有 `.docmind/` 内容都是「陷阱」，下面这些是**运行时数据，本体没有**，
要用只能从这里取：

| 内容 | 路径 | 用途 |
|---|---|---|
| 用户提问原文 | `.docmind_memory.sqlite3` → `checkpoints.question` | 真实语料（trace 只存长度不存原文，出于隐私） |
| trace | `.docmind_traces.jsonl` | 4531 行，**只有 `question_chars`，没有原文** |
| 项目记忆 | 同库 `memories` / `user_profiles` | 用户偏好、目标 |

⚠ 这些也都被 `.gitignore` 忽略，**同样会随 reaper 消失**。
要用就先备份，别假设它明天还在。
