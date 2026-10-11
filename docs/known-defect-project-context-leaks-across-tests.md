项目上下文跨测试文件泄漏 —— 立档（既存缺陷，非本次改动引入）。

## 一句话

`api._request_project_id()` 会回落 `projects.current_project_id()`，
后者读**进程级持久状态**（`projects.set_current` 写进注册表）。
测试文件各有各的沙箱，但「当前项目」**跨文件存活**，
于是 A 文件注册的项目 id 被拿去过滤 B 文件沙箱里的会话。

## 实测证据（探针打在第二个文件里，不是推测）

跑 `test_session_owner_project.py` + `test_session_restore_qa.py`，
在第二个文件的用例 `test_list_endpoint_still_works_and_reflects_delete`
开始前读状态：

    [PROBE] alive
    [PROBE] _request_project_id='prj-a2a39b6311dc'
    [PROBE] current_project_id='prj-a2a39b6311dc'
    [PROBE] SESSIONS_DIR=D:\Temp\docmind_test_state_xgw23icw\sessions
    [PROBE] 裸列出行数=0
    [PROBE] 按 pid=prj-a2a39b6311dc 过滤后=0

pid 非空、而 `SESSIONS_DIR` 是**另一个文件**的沙箱 —— 泄漏成立。

## 为什么干净 HEAD 上不红

旧规则是「无归属一律放行」：不管 pid 是谁，无归属会话都放行。
于是这个泄漏被**掩盖**了 —— 它一直在那儿，只是没人看见。

收窄之后它显形：`test_session_restore_qa.py::A5` 存了一个无归属会话，
`/api/sessions` 用 `_request_project_id()` 过滤，而那个 pid 来自别的文件，
于是这条会话被滤掉。

**我的改动没有制造这个泄漏，只是让一个既存缺陷不再被掩盖。**

## 为什么现在不修

今晚的纪律：**不同族的缺陷不得混在一次改动里。**
本次提交是「丙方案豁免收窄」；这是「项目上下文没隔离」。混在一起会让
提交说明讲不清改了什么，也会让回归归因变难。

## 根因（2026-10-12 追到底了）

不是「current 没复位」，是**沙箱按进程隔离，不按文件隔离**：

```python
# config.py:38
STATE_ROOT = os.getenv("DOCMIND_STATE_ROOT") or BASE_DIR
```

这行在 **import 那一刻**执行。第二个测试文件设自己的 env 已经太晚 ——
`config` 在 `sys.modules` 里，`import config` 直接返回缓存。

实测（B 文件里打印）：

```
[B] config already in modules = True
[B] STATE_ROOT = D:\Temp\docmind_test_state_b88gju4u
[A] STATE_ROOT = D:\Temp\docmind_test_state_b88gju4u     <- 同一个
```

### 顺带查出一个真实的空操作

`tests/test_session_owner_project.py:39`：

```python
os.environ["DOCMIND_STATE_DIR"] = str(_SANDBOX)   # ← 这个 env 名不存在
```

旁边注释也写着「先设 `DOCMIND_STATE_DIR` 让 STATE_ROOT 落到沙箱」。

但 `config.py` **只认 `DOCMIND_STATE_ROOT`**（第 38 行 / 第 73 行），
全仓没有第二处读 `DOCMIND_STATE_DIR`。**那行什么也没做** ——
文件以为自己设了沙箱，实际走的是进程级自动沙箱。

## 试过四种修法，全部无效（别重做）

| # | 修法 | 实测结果 |
|---|---|---|
| 1 | `tests/conftest.py` + `pytest_collectstart` 摘 `sys.modules['config']` | ❌ **太晚** —— 拿到 collector 时模块已 import 完 |
| 2 | 同上 + `pytest_collect_file` | ❌ **根本没触发** —— 实测 B 收集时 `config in sys.modules=True`，没被摘掉 |
| 3 | 同上 + `pytest_pycollect_makemodule` | ❌ 触发了、`config` 也摘了，但 `STATE_ROOT` 仍是旧的 |
| 4 | 测试侧 `teardown_module` 复位 `current_project_id` | ❌ **回落到注册表第一项**，照样泄漏 |

第 4 条的反直觉之处：

```python
# projects.current_project_id()
cid = _stored_current()
if cid and cid in reg: return cid
rows = list_projects()
return rows[0]["project_id"] if rows else ""     # ← 注册表非空就必然返回某个
```

**「把 current 复位成空」根本没用** —— 会被注册表里的第一个补上。
实测：`[A-teardown] 复位为 'prj-5344bf03948f'`（还是同一个）。

### 治本点与它的代价

真正要治的是**沙箱按文件隔离**，而那要求 `config.STATE_ROOT` 不再是
import 时定死的模块级常量。

**这个常量在全仓有 133 处引用、分布在 46 个文件。** 用户 2026-10-12
拍板：**只立档，不修**。理由是它不影响产品（只让一个测试在组合跑时红，
单独跑绿），而改动面覆盖 46 个文件，风险远大于收益。

### 如果将来要修，有一条更小的路（未验）

第 1~3 种修法都在**收集阶段**摘模块，失败的原因是别的模块持有
`config` 的引用。若改成在**每个测试文件自己的 `setUpModule` 里**
按「本文件是否设了 env」来决定摘不摘，时机上就没问题 ——
但那意味着 13 个自建沙箱的文件都要改，漏一个就还会踩。

## 判据

`tests/test_project_context_isolation.py`（3 条）钉的是**当前事实**，
不是「应该修好的样子」：

* `config.py` 里**不**出现 `DOCMIND_STATE_DIR`
* 那个测试文件**仍**用 `DOCMIND_STATE_DIR` 赋值（改对了就会红，
  提醒更新文档）
* 两个文件**共用**一个 `STATE_ROOT`（真子进程验）

**做不到的要求不该写进判据** —— 那只会制造一条永远红的测试。

## 复现命令

```powershell
.venv\Scripts\python.exe -B -X utf8 -m pytest `
    tests/test_session_owner_project.py tests/test_session_restore_qa.py `
    -q -p no:cacheprovider --no-header --timeout=300 --timeout-method=thread
# -> test_list_endpoint_still_works_and_reflects_delete 失败
```

单独跑第二个文件是绿的 —— **只有组合跑才红**，这也是它一直没被发现的原因。

## ⚠ 取证过程中我自己犯的两个错（记下来）

1. 第一版探针 `sys.stderr.write("...%r" % ())` —— 空元组当占位符，
   插件整个崩掉，输出里一行 `[PROBE]` 都没有。
   **探针坏掉时不能拿它的沉默当证据** —— 我差点据此说「没泄漏」。
2. 判据方向写反：`STEP2` 打空串、`STEP3` 看到了会话，那正是**没泄漏**，
   我却当成泄漏的证据。修正版加了无条件自证行 `[PROBE] alive`。
