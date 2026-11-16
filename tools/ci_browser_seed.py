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
    # seed_shared 对已存在文件不改 status（旧版 seed 留下的 candidate 会一直
    # 进不了 active 目录）；这两个文件是本夹具自有产物，重建保证幂等。
    for body in ("CI 共享正文", "第二类共享记忆：独立措辞供未注入对照与拖拽邻域使用"):
        fixture = os.path.join(promote.shared_dir("fact"),
                               promote.item_id("fact", body) + ".md")
        if os.path.isfile(fixture):
            os.remove(fixture)
    shared_idle = promote.seed_shared("fact", "第二类共享记忆：独立措辞供未注入对照与拖拽邻域使用",
                                      title="CI 共享记忆乙", status="active")
    # 边必须写进源文件 header：体检的索引重建按文件 header.edges 重同步 edges 表，
    # 只写 edges 表（set_edges）会在重建时被抹掉（2026-11-17 CI 首跑实测）。
    shared_used = promote.seed_shared(
        "fact", "CI 共享正文", title="CI 共享记忆", status="active",
        meta={"edges": [{"type": "refines", "to": shared_idle}] if shared_idle else []})
    # 图上的边只认「目录节点 ∩ index 镜像」：shared/project 候选住在 promote 文件里，
    # 没有 index 镜像就没有 scope，边会被项目过滤丢掉（2026-11-17 实测）。
    for mid, body, title in ((shared_used, "CI 共享正文", "CI 共享记忆"),
                             (shared_idle, "第二类共享记忆：独立措辞供未注入对照与拖拽邻域使用", "CI 共享记忆乙")):
        if mid:
            index.upsert_item({"id": mid, "kind": "fact", "scope": "global", "project": "",
                               "status": "active", "path": "shared/fact/%s.md" % mid,
                               "title": title, "body": body, "owner": "main"})
    cand = promote.seed_project(PROJECT, "fact", "CI 项目候选正文", title="CI 项目候选")
    if cand:
        index.upsert_item({"id": cand, "kind": "fact", "scope": "project", "project": PROJECT,
                           "status": "candidate",
                           "path": "projects/%s/fact/%s.md" % (PROJECT, cand),
                           "title": "CI 项目候选", "body": "CI 项目候选正文", "owner": "main"})
    context_panel.add_hook(PROJECT, SID, "note", "CI 钩子正文")
    context_panel.add_hook(PROJECT, SID, "note", "CI 钩子正文乙")
    if shared_used and shared_idle:
        index.set_edges(shared_used, [{"to": shared_idle, "type": "refines"}])
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