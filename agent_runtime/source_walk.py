"""项目源码遍历的**统一剪枝口径**。

来历（2026-11-23 的后端体检）：仓库里有四套各写各的目录排除清单
（`ingest._SKIP_DIRS`、`code_intel._SKIP_DIRS`、`game_workbench` 里就地写的四份
字面量），彼此不一致：

1. `game_workbench.asset_dependencies` / `impact_analysis` 的清单**漏了
   `node_modules`**；
2. 四份清单**都漏了 `.docmind` 状态目录**（`project_stages` / `projects` 等
   工作流副本与整仓影子副本，实测 8 万+ 文件），而 `ingest` 与 `code_intel`
   是按名剪这些目录的；
3. 于是 `/api/asset_dependencies` 在本机要 **196 秒**（实测），
   `/api/validate_data` 要 11.7 秒，且 `validate_data` 会把 `.tmp` 下
   临时 JSON 报成「项目配置损坏」——**假阳性**，不是项目的问题。

这里把口径收敛到一处：`walk_source_files()` 复用 `ingest._SKIP_DIRS`
（依赖/构建产物/版本控制/虚拟环境/向量库自身/归档备份）+ 剪掉所有点目录
（但放行 `.docmind` 本身，因为它的产物目录 `web/` 等要照常可见），再在
`.docmind/` 直接下一层按名剪掉状态目录 —— 与 `ingest` 的建库口径、
`code_intel` 的 grep 口径、`tools` 的 glob 口径完全一致。

放在 `agent_runtime` 下（而不是根目录）是因为它只依赖标准库，且不 import
任何业务模块 —— `game_workbench` 与 `ingest` 都能安全引用，不会成环。
"""

from __future__ import annotations

import os

# 依赖/构建产物/版本控制/虚拟环境/向量库自身/归档备份。
# 与 `ingest._SKIP_DIRS` 保持一致（那边是索引层的真源）；
# `code_intel._SKIP_DIRS` 是 grep 层的另一份，两边本来就允许略有出入，
# 这里以索引层为准 —— 索引层剪得更全，grep 层多剪的项对本模块无害。
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist",
    "bin", "obj", "Library", "Temp", ".idea", ".vscode", "target", "out",
    ".mypy_cache", ".ruff_cache",
    ".chroma", "_archived_builds", "uploads",
})

# `.docmind/` 是 Agent 工作台自己的状态根：产物目录（Godot Web 导出的
# `web/` 等）要照常可见，但状态目录（工作流副本、整仓影子副本）不扫。
# 名单与 `code_intel._DOCMIND_STATE_DIRS` 一致 —— 那里有原始出处与实测数据。
_STATE_DIRS = frozenset({
    "project_stages", "project_checkpoints", "projects", "projects-orphan-backup",
    "media-fixture", "page-action", "visual-evidence", "screenshots", "retrieval",
    "workflows", "cockpit-validation", "dependency-state", "skills",
})

# 动态命名的状态目录前缀（一次性的 visual 证据目录等）。
_STATE_PREFIXES = ("docmind-production-visual-",)

# 单个文件超过此体积（字节）就跳过。
#
# 为什么需要：状态数据可能是个超大 JSON —— 本机 `.docmind/context_compression.json`
# 有 39MB，读一遍 + 跑一遍正则就是 2.2 秒，是 `asset_dependencies` 剩下的最大
# 单笔开销。而「源码」本身不会这么大：真到这个量级的多半是状态导出、
# 截图产物或压缩包，不是人能写的代码。
#
# 上限与 `ingest._MAX_CODE_FILE`（500KB）、`code_intel._MAX_FILE_BYTES`（2MB）
# 同类。这里取 2MB：够宽松，同时把那种「一个文件吃掉整个请求」的情况挡住。
MAX_FILE_BYTES = 2 * 1024 * 1024


def _prune(dirs: list[str]) -> list[str]:
    """剪掉依赖/构建产物，并放行 `.docmind` 本身（点目录一律不扫）。"""
    return [d for d in dirs
            if d not in _SKIP_DIRS and (d == ".docmind" or not d.startswith("."))]


def walk_source_files(root: str, *, extra_skip: frozenset[str] | set[str] | None = None,
                       max_file_bytes: int | None = None):
    """产出 ``root`` 下的文件路径，按统一口径剪枝。

    与 `ingest` 的建库口径一致：剪依赖/构建产物/点目录，并在 `.docmind/`
    直接下一层按名剪掉状态目录。`extra_skip` 供调用方再叠一层自己的排除项
    （例如引擎专属的 ``Library`` / ``Intermediate``）。

    `max_file_bytes` 覆写单文件体积上限（默认 `MAX_FILE_BYTES`）；传 ``0``
    表示不限。
    """
    skip = _SKIP_DIRS | set(extra_skip or ())
    limit = MAX_FILE_BYTES if max_file_bytes is None else int(max_file_bytes)
    root_abs = os.path.abspath(root) if root else ""
    if not root_abs or not os.path.isdir(root_abs):
        return
    for dirpath, dirnames, filenames in os.walk(root_abs):
        dirnames[:] = [d for d in dirnames
                       if d not in skip and (d == ".docmind" or not d.startswith("."))]
        # 仅在 `.docmind/` 直接下一层按名剪状态目录（产物目录照常可见）。
        if os.path.relpath(dirpath, root_abs).replace("\\", "/") == ".docmind":
            pruned = [d for d in dirnames
                      if d in _STATE_DIRS or d.startswith(_STATE_PREFIXES)]
            if pruned:
                dirnames[:] = [d for d in dirnames if d not in pruned]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if limit > 0:
                try:
                    if os.path.getsize(full) > limit:
                        continue
                except OSError:
                    continue
            yield full
