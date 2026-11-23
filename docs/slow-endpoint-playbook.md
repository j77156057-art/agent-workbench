---
title: 慢接口排查手册
description: 端点普查 → profile → 定位重复劳动 → 证明输出一致 → 回归
when_to_use: 「哪个接口慢」「后端有什么可以优化的」「做一轮性能体检」
---

# 慢接口排查手册

2026-11-23 的两轮后端体检沉淀下来的流程。每一步都有本机实测数据支撑。

## 0. 先做受控普查，别凭印象挑目标

扫一遍所有 GET 端点看谁慢，但**必须受控**——第一次普查就因为没受控把服务打挂了。

```python
# 串行；每端点独立超时；独立连接；失败隔离
req = urllib.request.Request(BASE + path, method="GET", headers={"Connection": "close"})
try:
    resp = urllib.request.urlopen(req, timeout=20)
except socket.timeout:
    ...  # 记为 TIMEOUT，继续下一个
```

必守的四条：

| 做法 | 原因 |
|---|---|
| 串行，不并发 | 并发让计时互相污染，且更容易打挂服务 |
| 每端点独立超时 | 一个慢端点不能拖垮整轮 |
| `Connection: close` | keep-alive 连接会被慢端点长期占着 |
| 跳过会起外部进程/长轮询的端点 | 如 `/api/runtime/events` 会占住连接不放 |

**每端点跑两遍（冷 + 热）**，两遍的差别往往就是答案：`build_symbol_map`
首调 3.3 秒、热调 45ms——那是**一次性初始化**，不是真慢。

## 1. 分清「我们的慢」和「别人的慢」

普查结果里有一整簇接口恰好都在 2050ms 左右——这个整齐的数字说明是**共同原因**。
查下去是 ComfyUI 没装、Unreal 桥接超时：**外部服务缺席**，不是我们代码的问题，
优化不了也不该优化。

对照 `/api/assets/search` 热态 10 秒——那是打 Poly Haven 官网的**外部网络请求**，
同样不是我们的代码。

判据：看响应体。`{"ok":true,"available":false,"error":"...拒绝连接"}` 就是外部缺席。

## 2. profile，不要猜

本轮两次靠猜都错：

- 以为 `build_symbol_map` 每次都慢 → 实际是首调初始化
- 以为换掉 `ast.walk` 的实现更快 → 实测**更慢**（纯 Python DFS 打不过 C 实现的 ast.walk）

profile 一跑，热点直接现形：

```
ncalls  tottime  cumtime  名称
7736    0.027   2.528   workbench_fs.py:278(_find_repo)
7737    0.045   2.024   <frozen ntpath>:663(realpath)
15474   1.918   1.918   {built-in method nt._getfinalpathname}
```

## 3. 找「重复劳动」的信号

最常见的慢不是算法慢，是**同一份工作做了两遍**。信号：

- profile 里同一个函数的调用次数 ≈ **文件数 × 2**
- 逐节点做一件可以按目录/按文件缓存的事

本轮两例：

1. `_git_fields` 对每个节点调 `os.path.realpath`（7736 次）——但节点里绝大多数是
   文件，它们共享同一个父目录，父目录的 realpath 可以复用。
2. 同一个 .py 被 `ast.parse` 两遍（`symbols._extract_python` 一遍、
   `build_relation_graph` 再一遍）——全仓 799 个 .py，多花 3.2 秒。

## 4. 测性能必须用重复测量

首次测量混入磁盘冷缓存，会得出**相反**的结论：

```
单遍 ast.parse 799 文件：第一次 6570ms，第二遍（同样代码）3591ms
  —— 单遍竟然比双遍慢，纯粹是首次读盘冷缓存
```

规矩：先跑一遍预热，再连测 3 次取热态。

## 5. 证明「输出一致」，再谈提速

缓存类优化最硬的证据：**把旧版从 git HEAD 取出，在独立子进程里各跑一遍，
JSON 排序后逐项比对**。

```python
old = git_show("HEAD:workbench_fs.py")   # bytes decode，别用 text=True
# 复制到 .tmp/baseline/ 与 .tmp/current/，各自独立子进程跑，
# print(json.dumps(out, sort_keys=True, default=str)) 后逐项比
```

本轮 build_tree / build_symbol_map / build_relation_graph 三项**逐字节完全一致**。

坑：Windows 上 `subprocess.run(..., text=True)` 用 GBK 解码，git 输出里的中文/二进制
会抛 `UnicodeDecodeError`——用 `capture_output=True` 拿 bytes 再 `.decode("utf-8", "replace")`。

## 6. 缓存必须会失效

缓存键要含**内容指纹**（mtime_ns + size），不能只用路径；文件改了必须重新解析。
写操作后要清缓存（本项目 `invalidate_status()` 同时清 git 快照缓存与 realpath 缓存）。

回归测试要专门锁这一条：改文件 → 断言拿到的是新 AST。

## 7. 语义等价要证明，不要「应该一样」

把 `realpath` 换成纯字符串前缀匹配能再快 5 倍，但**在 junction/符号链接下不等价**：
旧实现能穿透 junction 找到仓库，前缀匹配找不到。

所以选了「目录级 realpath 缓存」——保留 realpath 语义，只把调用次数从
「每文件一次」降到「每目录一次」。

Windows 上建不了 symlink（需管理员），没法实测这个场景——那就**别选那个方案**，
而不是假设它没问题。

## 8. 收尾

- 新增回归测试锁住：口径一致、缓存会失效、边界返回 None
- 端点级复测（不是只测函数）：同机对照两个实例
- 推 CI 让 4 个 shard 跑全量（本地全量在 495 个测试文件的仓库会超 1 小时上限）
- CI 红了先查是不是自己改的：看失败步骤的日志，看它是不是环境性偶发
  （本轮 `Browser preflight` 超时过一次，重跑就绿了——runner 上浏览器启动竞争，
  与改动无关；判断依据是失败步骤与改动有没有交集）

## 附：本仓库的关键位置

| 位置 | 作用 |
|---|---|
| `agent_runtime/source_walk.py` | 项目源码遍历的**统一剪枝口径**（依赖/构建产物/点目录/.docmind 状态目录 + 2MB 单文件上限） |
| `ingest._SKIP_DIRS` | 索引层的排除清单（真源） |
| `agent_runtime/code_intel.py::_DOCMIND_STATE_DIRS` | `.docmind` 状态目录名单 |
| `symbols._ast_cache` | 共享 Python AST 缓存 |
| `workbench_fs._realpath_cache` | 目录级 realpath 缓存 |

