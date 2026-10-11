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

## 两条修法（各有代价，需选）

### A. 在测试侧清理（治标，但风险最低）

每个碰项目的测试文件在 `tearDownModule` 里把当前项目复位：

```python
import projects
_old = projects.current_project_id()
# ... 测试
projects.set_current(_old)     # 或 projects.clear_current()
```

* 代价：要逐个文件加，`tests/` 里碰项目的有 20+ 个文件
* 风险：漏一个就还会漏；治的是「测试没清理」，不是「生产没隔离」

### B. 在生产侧收紧回落（治本，但要确认产品语义）

`_request_project_id()` 不再回落 `current_project_id()`，只认
**显式请求上下文**（`_ctx_project_id()`）。没有显式上下文就没有项目。

* 好处：测试之间天然隔离；也让「没指定项目就不该按某个项目过滤」
  这条语义在生产里也成立
* 代价：**可能改变真实产品行为** —— 前端/桌面壳有些路径靠
  「当前项目」兜底，那些会开始收到未过滤的结果（= 看到全部会话）
* 必须先盘清「谁在依赖这个回落」，不能直接改

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
