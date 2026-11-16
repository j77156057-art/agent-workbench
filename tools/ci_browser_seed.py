"""CI 浏览器回归的确定性记忆夹具：四个 store 各至少一行 + 一条带注入快照的 trace。

memoryLayers.browser.mjs 的断言是「DOM 与 API 口径一致 + 注入/未注入亮度差」，
需要真实存在的库存与一条注入记录；空状态跑不了。本脚本用后端模块真写（不伪造
API 响应），配合 DOCMIND_STATE_ROOT 隔离目录与 DOCMIND_MEMORY_V2=1 使用：

    DOCMIND_STATE_ROOT=<state> DOCMIND_MEMORY_V2=1 python tools/ci_browser_seed.py <project_id>

输出会话 id（供 DOCMIND_LAYER_SESSION / DOCMIND_HOOK_SESSION）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_memory
import agent_trace
import sessions
from agent_runtime.memory import context_panel, index, promote

PROJECT = sys.argv[1] if len(sys.argv) > 1 else "prj-cibrowser"
SID = "web-cibrowser01"
NOW = time.strftime("%Y-%m-%dT%H:%M:%S")


def main():
    scope = agent_memory.scope_key(PROJECT)
    legacy_used = agent_memory.remember(scope, "fact", "CI 已注入事实", "CI 已注入正文",
                                        agent_id="main", importance=0.7)
    agent_memory.remember(scope, "fact", "CI 未注入事实", "CI 未注入正文",
                          agent_id="main", importance=0.6)
    shared_used = promote.seed_shared("fact", "CI 共享正文", title="CI 共享记忆", status="active")
    promote.seed_shared("fact", "CI 共享正文乙", title="CI 共享记忆乙", status="active")
    # project 层注入判定看内容是否进共享注入块：P1 与共享正文同文 → used。
    index.upsert_item({"id": "ci-project-1", "kind": "fact", "scope": "project",
                       "project": PROJECT, "status": "active", "path": "ci/seed.md",
                       "title": "CI 项目记忆", "body": "CI 共享正文", "owner": "main",
                       "importance": 0.7})
    index.upsert_item({"id": "ci-project-2", "kind": "fact", "scope": "project",
                       "project": PROJECT, "status": "active", "path": "ci/seed2.md",
                       "title": "CI 项目记忆乙", "body": "CI 项目正文乙", "owner": "main",
                       "importance": 0.6})
    context_panel.add_hook(PROJECT, SID, "note", "CI 钩子正文")
    context_panel.add_hook(PROJECT, SID, "note", "CI 钩子正文乙")
    if shared_used:
        index.set_edges(legacy_used, [{"to": shared_used, "type": "refines"}])
    sessions.save(SID, [{"user": "CI 夹具问题", "assistant": "CI 夹具回答", "ts": NOW}],
                  project_id=PROJECT)
    agent_trace.record({
        "turn_id": "cibrowser0001", "ts": NOW, "session_id": SID, "agent_id": "main",
        "provider": "mock", "model": "mock", "steps": [], "actions": [],
        "injection": [
            {"layer_key": "legacy", "item_ids": [legacy_used], "text": "CI 已注入正文",
             "chars": 7, "hash": "ci-seed", "version": 1},
            {"layer_key": "shared", "text": "CI 共享正文", "chars": 6, "hash": "ci-seed", "version": 1},
            {"layer_key": "hook", "text": "CI 钩子正文", "chars": 6, "hash": "ci-seed", "version": 1},
        ],
    })
    print(SID)


if __name__ == "__main__":
    main()