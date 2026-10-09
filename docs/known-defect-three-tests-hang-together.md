# 缺陷立档：三个测试文件在连跑时会挂死（父子进程互等）

> **状态：已定位到用例/文件/行号，未修。**
> 影响：全量回归 `pytest tests/` **永远跑不完**（CPU 空转数小时、输出不动），
> 于是所有人都会开始忽略回归结果 —— 这比几个失败用例严重得多。

## 1. 怎么定位到的

逐文件跑（每个文件一个 pytest 进程、240 秒超时真杀），314 个文件里
**只有 3 个超时**：

| 文件 | 结果 | 用例数 |
|---|---|---|
| `tests/test_agent_trace.py` | **240s 超时** | 26 |
| `tests/test_native_prose_final.py` | **240s 超时** | 7 |
| `tests/test_parallel_native_not_stall.py` | **240s 超时** | 10 |

同类的 `test_agent.py` / `test_agent_capacity.py` 都是 0.7 秒完成，
所以**不是「agent 测试都慢」**。

## 2. 挂死时的现场

用 `pytest -s` + `PYTHONUNBUFFERED=1` 关掉缓冲，实时看进度点：

```
已过的用例点数 = 25（该文件共 26）—— 前 25 个全过，卡在第 26 个
末 3 行（卡住时的调用栈）:
  File "<frozen importlib._bootstrap>", line 1360 in _find_and_load
  File "D:\WorkBuddy\rag-agent\tools.py", line 38 in <module>
子进程: 另一个 pytest，命令行是同一个文件的完整副本
```

**形态是：pytest 起了自己的子进程，子进程卡在 `import` 阶段。**

## 3. 链条

1. 第 25 个用例 `test_tool_step_is_recorded`（`tests/test_agent_trace.py:166`）
   的脚本是 `Action: read_file / Action Input: path: agent.py` ——
   **它真的调了工具**，于是首次导入 `tools.py`
2. `tools.py:38` 是 `from agent_runtime.retrieval import get_retriever`
   —— 这一步会建 embedding / 向量库客户端
3. 初始化过程中，`tools.py:7876` / `:7957` 这一类代码用
   `interpreter = ... else sys.executable` 起了子 python
   （`python_exec` / `dev_serve` / `run_command` 一族）
4. 子进程从零导入 `agent` / `tools`，撞上同一处初始化
   → **父子互等，挂死**

## 4. 为什么「单跑那个用例」是快的

单独跑第 26 个（`test_truncation_retry_suppresses_thinking`）**1.4 秒通过**。
因为它不经过前 25 个的累积状态（首件导入 `tools.py`、初始化 retrieval 等）。

**这也是最难查的原因**：没有任何单个用例是慢的，慢只出现在连跑里。

## 5. 尚未验证的部分（接手先看这里）

* 到底是 `tools.py` 哪一处起了子进程 —— 我定位到
  `:7876` / `:7957` 两处 `sys.executable`，但**没确认是它们**
* 子进程挂死在 retrieval 初始化的哪一步（embedding 客户端？Chroma？GPU 租约？）
* 另两个文件（`test_native_prose_final.py` / `test_parallel_native_not_stall.py`）
  是否同一条链 —— **只观测到同样超时，未验证同因**

## 6. 建议的查法（比继续猜快）

给 pytest 加 `-p no:cacheprovider` 之外，再加
`--timeout`（**需先 `pip install pytest-timeout`** —— 本机没装，
我第一版猎挂脚本就因为传了 `--timeout` 而全盘作废：每个文件 0.7 秒
以 `unrecognized arguments` 退出，314 个 `nosummary` 是假象）。

装好后：

    pytest tests/test_agent_trace.py --timeout=30 --timeout-method=thread -x

超时会被抓成失败并打出当时的栈，比等 240 秒再杀更能拿到现场。

或者在 `tools.py` 那几处 `sys.executable` 前加一句日志（打 `argv`），
跑一次就能确定是谁起的子进程。

## 7. 附带教训（我自己的）

* 第一版猎挂传了 `--timeout=180` 但插件没装 → 314 个假 `nosummary`。
  **没先验证依赖存在就用了它。**
* 追进程查了两次都在错误时间点（一次已退出、一次被我自己 `Stop-Job` 带走）。
  最后是靠 `-s` 关缓冲才拿到实时进度 —— **该早这么做**。
* `pytest -q` 的 stdout 有缓冲，被杀时刷不出进度点，所以「日志几乎空」
  不等于「什么都没发生」。这里 0.1KB 的日志其实含 25 个点数。

## 相关

* `tools/mutation/` 下无本次产物（已清）
* `D:/Temp/opencode/perfile/perfile_summary.txt` —— 逐文件全量结果
* `D:/Temp/opencode/perfile/*.timeout.log` —— 三个超时文件的现场
