# 已知缺陷：`*_external_file` 三个工具未登记进权威分类表

**状态**：待修（已确认为真缺陷，与「子代理自定义工具集」那次改动无关）
**发现于**：`6431721` 之后那次子代理改动（2026-10-06）
**发现方式**：为「只读角色不能改东西」找一个权威判据时，`agent_runtime.tools`
的分类表里找不到它们。

## 现象

这三个工具**真的会改磁盘上的文件**，但不在任何一张权威分类表里：

```
create_external_file
edit_external_file
delete_external_file
```

逐张表查的结果都是空：

```python
>>> for n in ("create_external_file", "edit_external_file", "delete_external_file"):
...     print(n, [t for t in tables if n in getattr(reg, t, ())])
create_external_file   []
edit_external_file     []
delete_external_file   []
```

`agent_runtime/tools.py` 里的权威表：

| 表 | 内容 |
|---|---|
| `WRITE_TOOLS` | `apply_edit` `create_artifact` `create_file` `dev_apply_edits` `dev_asset_register` `dev_capture_bug` `dev_desktop_action` `dev_preview_adapter_*` `dev_rebuild_index` `dev_refactor` `dev_region_edit` `dev_update_bug` `game_upsert_task` |
| `EXEC_TOOLS` | `dev_preview_adapter_test` `game_playtest` `python_exec` `run_command` `self_verify` |
| `ADMIN_TOOLS` | `dev_add_region` `dev_apply_regions` `dev_approve` `dev_commit` `dev_commit_all` `dev_desktop_action` `dev_install_tool` `dev_mcp_*` `dev_preview_adapter_approve` `dev_preview_adapter_rollback` `dev_preview_adapter_test` `dev_rollback_changeset` `init_regions` `start_workflow` |
| `NETWORK_TOOLS` | 含 `web_fetch` 等 |

`python_exec` / `run_command` 也不在 `WRITE_TOOLS` 里（在 `EXEC_TOOLS`）—— 所以
「只读」这件事**用任何单张表都表达不准**，这是发现这个缺陷的直接原因。

## 影响

1. **能力租约 / 审批门**（`capability_lease`、`approval`）按分类表判断要不要
   人工确认，这三个工具绕过判断。
2. **只读角色的约束**：在 `agent.py` 里按表拦写工具时，这三个拦不住——本次
   改动在 `_run_child` 里**显式补列**了这三个名字，但那是补丁，根子在分类表。
3. **`tests/test_tool_taxonomy.py::NoShadowTaxonomyTests` 已经警告过这类事**：
   > 「它不在运行路径上，断言它等于断言一份与真实行为无关的清单——这正是
   > `dev_apply_edits` 漏登记却一直绿灯的原因。」

   `dev_apply_edits` 后来登记进去了，这三个还没。

## 修之前该先做的事

**不要直接照着名字补进去就算完。** 按本仓库的规矩，先探针后断言：

1. 真跑一次这三个工具，确认它们到底改了什么（哪些路径、在哪个根下）
2. 确认它们属不属于 `WRITE_TOOLS`（改本地磁盘）还是需要新的一档
   （`IRREVERSIBLE_TOOLS`？外部文件删除不可逆）
3. `NETWORK_TOOLS` 里 `web_fetch` 是否也该被算进「会改东西」——它能写文件吗？
4. 补完登记后，`tests/test_tool_taxonomy.py` 的那条 `NoShadowTaxonomyTests`
   要能防止**下一个**同类漏登记（目前它只防「agent.py 出现影子副本」，
   不防「权威表漏登记」）

## 现有的临时兜底

`agent.py` 的 `_run_child` 里，只读角色的工具拦截显式补列了这三个名字：

```python
_NOT_ALLOWED_READ_ONLY = (
    set(_T_WRITE) | set(_T_EXEC) | set(_T_ADMIN)
    | {"create_external_file", "edit_external_file", "delete_external_file"}
)
```

那是补丁不是根因。根因修好后这行应该删掉，让它走权威表。
