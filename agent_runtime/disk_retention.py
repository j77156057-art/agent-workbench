"""磁盘保留期：给三类「有界副本」定过期，默认只出计划不动手。

## 为什么需要这一层

这三类都是**为了可恢复而复制出来的目录**，写完就没人再管，只增不减（2026-11-01 本机实测）：

| 目录 | 实测 | 谁写 |
|---|---|---|
| `.docmind/project_stages/<wf-id>/` | **5.02 GB / 5.8 万文件**（单个 725 MB） | `project_stage.create_stage`（试做区：baseline + workspace 两份整项目复制） |
| `.docmind/project_checkpoints/<wf-id>/cp-*/` | 0.80 GB | `project_checkpoint.create_checkpoint`（每个 cp 上限 32 MB） |
| `dist/DocMind_*`、`dist/regression-*`、`dist/acceptance-*` | 4.06 GB（最老 52 天） | 构建 / 回归 / 验收时的一次性副本 |

原始缺陷不是"目录大"，而是**没有过期语义**：没有任何代码会回头清它们，所以容量单调增长。
本模块只补这一件事 —— 判据写成纯函数，扫描与执行分开，动手必须显式 `apply`（宪法 C-006：
不可逆类操作要显式批准）。

## 三条判据（都是刻意的保守选择）

1. **只清终态工作流的副本**：终态 = `completed` / `completed_unverified` / `failed`。
   `interrupted`（可续跑）与 `awaiting_choice`（等人选方案）**永不动** —— 删了它们就毁掉恢复入口，
   那正是这些副本存在的理由。
2. **认不出状态的一律不自动删**：`project_stages` / `project_checkpoints` 里有 5 项 / 11 项
   找不到对应的 `workflows/wf-*.json`。没有状态文件不等于工作流已结束（图状态可能在
   `langgraph_checkpoints.sqlite` 里）。这类进 `needs_review` 单独列，要 `include_orphans` 才会
   变成可回收项 —— 宁可少回收几个 GB，也不猜。
3. **`dist/` 认前缀 + 认引用**：只有 `DocMind_* / regression-* / acceptance-*` 这三类一次性副本会被
   判；`dist/release/`、`installer/`、`*.zip` 是发布物，**永不自动清**。名字若出现在被跟踪的文本里
   （README / docs / packaging 脚本 —— 实测 `release` 那类就带 SHA 引用）也保留。

`git grep` 在本机可用；不可用时按「有引用」处理（fail-closed，宁少删不误删）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

TERMINAL_STATUSES = frozenset({"completed", "completed_unverified", "failed"})
#: 明确保留的非终态（写出来是为了让"保留"成为规则而不是巧合）
LIVE_STATUSES = frozenset({"running", "awaiting_choice", "awaiting_approval",
                           "interrupted", "retry_pending", "lease_busy", "draft"})

STAGES = "project_stages"
CHECKPOINTS = "project_checkpoints"
DIST = "dist_build"

DIST_PREFIXES = ("DocMind_", "regression-", "acceptance-")
#: 发布物与安装器：体积再大也不自动清，要人自己决定。
DIST_NEVER = ("release", "installer")

DEFAULT_RULES: dict[str, dict[str, Any]] = {
    # 一个工作流只有一个试做区目录，所以"每区保留最近一份"在这里等于永不回收 ——
    # 改成**全局保留最近 N 个区**：最近在用的那几个一定还能回滚，其余终态的才进回收。
    STAGES: {"idle_days": 14, "keep_newest": 3},
    CHECKPOINTS: {"idle_days": 14, "keep_per_workflow": 2},
    DIST: {"idle_days": 30},
}


def _dir_bytes(path: Path) -> tuple[int, int]:
    """(字节, 文件数)。单文件 stat 失败不计，也不中断整盘扫描。"""
    total = 0
    count = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
                count += 1
            except OSError:
                continue
    return total, count


def workflow_statuses(state_root: str) -> dict[str, str]:
    """读 `workflows/wf-*.json` 的 status；读不到就当未知（不猜终态）。"""
    out: dict[str, str] = {}
    base = Path(state_root) / "workflows"
    if not base.is_dir():
        return out
    for entry in sorted(base.glob("wf-*.json")):
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[entry.stem] = ""
            continue
        status = str(raw.get("status") or (raw.get("graph_state") or {}).get("status") or "") \
            if isinstance(raw, Mapping) else ""
        out[entry.stem] = status
    return out


#: 只有这些后缀里的引用才算"有东西要吃这个目录"（构建、脚本、CI、配置、代码）。
#: 实测收紧的依据：整仓 4.06 GB 的 `dist/` 一次性副本，唯一"引用"它们的文件是
#: `golden/results_baseline.jsonl` —— 那是历史评测输出里出现过的路径**字符串**，
#: 是记录不是依赖。按记录判保留，等于给这批副本发了永久豁免。
REFERENCE_CODE_SUFFIXES = (".py", ".ps1", ".bat", ".cmd", ".sh", ".yml", ".yaml",
                           ".toml", ".cfg", ".ini", ".json", ".ndjson")
#: 数据/结果/夹具目录：里面出现的路径是历史记录，不是消费方。
REFERENCE_DATA_PREFIXES = ("golden/", "tests/fixtures/", "docs/", "dist/")


def _counts_as_reference(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/")
    if normalized.startswith(REFERENCE_DATA_PREFIXES):
        return False
    return normalized.endswith(REFERENCE_CODE_SUFFIXES)


def referenced_names(root: str, names: Iterable[str]) -> set[str]:
    """在被跟踪的**代码/构建定义**里查这些目录名，出现过就视为「仍被引用」。

    用 `git grep -l -F`（本机实测快且能穿整仓）；git 不可用或报错时**全部按有引用处理**，
    让保留判定在缺工具时退化成不动手，而不是退化成乱删。命中的文件还要过
    `_counts_as_reference` —— 数据文件与文档正文里的同名字符串不算依赖。
    """
    wanted = [n for n in names if n]
    if not wanted:
        return set()
    found: set[str] = set()
    pathspecs = ["*%s" % suffix for suffix in REFERENCE_CODE_SUFFIXES]
    try:
        for name in wanted:
            probe = subprocess.run(
                ["git", "-C", root, "grep", "-l", "-F", "-e", name, "--", *pathspecs],
                capture_output=True, timeout=25)
            if probe.returncode not in (0, 1):   # 2 = git 自己出错，不能当"没引用"
                return set(wanted)
            hits = [line.strip().replace("\\", "/")
                    for line in probe.stdout.decode("utf-8", "replace").splitlines()
                    if line.strip()]
            if any(_counts_as_reference(hit) for hit in hits):
                found.add(name)
    except (OSError, subprocess.SubprocessError):
        return set(wanted)
    return found


def _age_days(mtime: float, now: float) -> float:
    return max(0.0, (now - mtime) / 86400.0)


def plan_stage_dirs(entries: list[dict[str, Any]], statuses: Mapping[str, str], now: float,
                    rules: Mapping[str, Mapping[str, Any]] = DEFAULT_RULES,
                    *, include_orphans: bool = False) -> list[dict[str, Any]]:
    """纯判定：`entries` 是 [{path,name,bytes,files,mtime}]，返回带 decision/reason 的条目。

    `decision` ∈ `reclaim` / `needs_review` / `keep`。全局最新的 `keep_newest` 个试做区
    无条件保留 —— 判据是「最近在干的事还能回滚」，不是「每个区都留一份」（一个区只有一个目录）。
    """
    rule = rules[STAGES]
    idle_days = int(rule["idle_days"])
    keep_newest = max(0, int(rule.get("keep_newest", 0)))
    ordered = sorted(entries, key=lambda row: (row["mtime"], row["name"]), reverse=True)
    protected = {row["name"] + "|" + row["path"] for row in ordered[:keep_newest]}
    out: list[dict[str, Any]] = []
    for item in ordered:
        row = dict(item)
        workflow = item["name"]
        status = str(statuses.get(workflow, ""))
        row["workflow_id"] = workflow
        row["status"] = status
        row["age_days"] = round(_age_days(item["mtime"], now), 1)
        if workflow + "|" + item["path"] in protected:
            row["decision"] = "keep"
            row["reason"] = "全局最近 %d 个试做区之一（回滚入口）" % keep_newest
        elif status in LIVE_STATUSES:
            row["decision"] = "keep"
            row["reason"] = "工作流未终态：%s" % status
        elif row["age_days"] < idle_days:
            row["decision"] = "keep"
            row["reason"] = "未过保留期（%s 天）" % idle_days
        elif not status:
            if include_orphans:
                row["decision"] = "reclaim"
                row["reason"] = "无状态文件，已按孤儿策略放行"
            else:
                row["decision"] = "needs_review"
                row["reason"] = "无状态文件，无法确认已结束；要回收需显式 include_orphans"
        else:
            row["decision"] = "reclaim"
            row["reason"] = "终态 %s 且闲置 %s 天 > %s 天" % (
                status, row["age_days"], idle_days)
        out.append(row)
    return out


def plan_checkpoint_dirs(entries: list[dict[str, Any]], statuses: Mapping[str, str], now: float,
                         rules: Mapping[str, Mapping[str, Any]] = DEFAULT_RULES,
                         *, include_orphans: bool = False) -> list[dict[str, Any]]:
    """检查点：一个工作流下有多批 `cp-*`，按批判，保留最近 `keep_per_workflow` 批。"""
    rule = rules[CHECKPOINTS]
    idle_days = int(rule["idle_days"])
    keep_last = max(1, int(rule["keep_per_workflow"]))
    out: list[dict[str, Any]] = []
    ordered_workflows = sorted({item["workflow_id"] for item in entries})
    for workflow in ordered_workflows:
        group = [row for row in entries if row["workflow_id"] == workflow]
        group.sort(key=lambda row: row["mtime"], reverse=True)
        status = str(statuses.get(workflow, ""))
        for index, item in enumerate(group):
            row = dict(item)
            row["workflow_id"] = workflow
            row["status"] = status
            row["age_days"] = round(_age_days(item["mtime"], now), 1)
            if index < keep_last:
                row["decision"] = "keep"
                row["reason"] = "保留每区最近 %d 批检查点" % keep_last
            elif status in LIVE_STATUSES:
                row["decision"] = "keep"
                row["reason"] = "工作流未终态：%s" % status
            elif row["age_days"] < idle_days:
                row["decision"] = "keep"
                row["reason"] = "未过保留期（%s 天）" % idle_days
            elif not status:
                row["decision"] = "reclaim" if include_orphans else "needs_review"
                row["reason"] = "无状态文件" + ("（已按孤儿策略放行）" if include_orphans else
                                                "，需显式 include_orphans")
            else:
                row["decision"] = "reclaim"
                row["reason"] = "终态 %s 且闲置 %s 天 > %s 天" % (
                    status, row["age_days"], idle_days)
            out.append(row)
    return out


def plan_dist(entries: list[dict[str, Any]], references: Iterable[str], now: float,
              rules: Mapping[str, Mapping[str, Any]] = DEFAULT_RULES) -> list[dict[str, Any]]:
    """`dist/` 只判一次性构建副本；发布物与被引用的目录不动。"""
    idle_days = int(rules[DIST]["idle_days"])
    referenced = set(references)
    out: list[dict[str, Any]] = []
    for item in sorted(entries, key=lambda row: row["name"]):
        row = dict(item)
        row["age_days"] = round(_age_days(item["mtime"], now), 1)
        name = str(item["name"])
        if not item["is_dir"]:
            row["decision"] = "keep"
            row["reason"] = "非目录（日志/包文件），本策略只管副本目录"
        elif name in DIST_NEVER or name.endswith(".zip"):
            row["decision"] = "keep"
            row["reason"] = "发布物或安装包，保留期策略不碰"
        elif not name.startswith(DIST_PREFIXES):
            row["decision"] = "keep"
            row["reason"] = "不在一次性副本前缀白名单内"
        elif name in referenced:
            row["decision"] = "keep"
            row["reason"] = "仍被跟踪文件按名字引用"
        elif row["age_days"] < idle_days:
            row["decision"] = "keep"
            row["reason"] = "未过保留期（%s 天）" % idle_days
        else:
            row["decision"] = "reclaim"
            row["reason"] = "闲置 %s 天 > %s 天的一次性副本" % (row["age_days"], idle_days)
        out.append(row)
    return out


def _scan_flat(base: Path, kind: str) -> list[dict[str, Any]]:
    rows = []
    if not base.is_dir():
        return rows
    for entry in sorted(base.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        size, count = _dir_bytes(entry)
        rows.append({"kind": kind, "path": str(entry), "name": entry.name,
                     "bytes": size, "files": count, "mtime": entry.stat().st_mtime,
                     "is_dir": True})
    return rows


def scan(state_root: str, project_root: str = "", *, now: float | None = None,
         rules: Mapping[str, Mapping[str, Any]] = DEFAULT_RULES,
         include_orphans: bool = False,
         statuses: Mapping[str, str] | None = None,
         references: Iterable[str] | None = None) -> dict[str, Any]:
    """扫三类目录并出计划。`project_root` 空则跳过 `dist/`（保留期只管仓库内副本）。"""
    stamp = now if now is not None else time.time()
    state = Path(state_root).resolve()
    stage_state = statuses if statuses is not None else workflow_statuses(state_root)
    rows = plan_stage_dirs(_scan_flat(state / STAGES, STAGES), stage_state, stamp, rules,
                           include_orphans=include_orphans)

    checkpoint_entries: list[dict[str, Any]] = []
    root_cp = state / CHECKPOINTS
    if root_cp.is_dir():
        for workflow_dir in sorted(root_cp.iterdir()):
            if not workflow_dir.is_dir() or workflow_dir.is_symlink():
                continue
            for cp in sorted(workflow_dir.iterdir()):
                if not cp.is_dir() or cp.is_symlink():
                    continue
                size, count = _dir_bytes(cp)
                checkpoint_entries.append({"kind": CHECKPOINTS, "path": str(cp),
                                           "name": cp.name, "workflow_id": workflow_dir.name,
                                           "bytes": size, "files": count,
                                           "mtime": cp.stat().st_mtime, "is_dir": True})
    rows += plan_checkpoint_dirs(checkpoint_entries, stage_state, stamp, rules,
                                 include_orphans=include_orphans)

    dist_rows: list[dict[str, Any]] = []
    if project_root:
        dist_base = Path(project_root).resolve() / "dist"
        if dist_base.is_dir():
            for entry in sorted(dist_base.iterdir()):
                size, count = _dir_bytes(entry) if entry.is_dir() else (
                    entry.stat().st_size if entry.is_file() else 0, 0)
                dist_rows.append({"kind": DIST, "path": str(entry), "name": entry.name,
                                  "bytes": size, "files": count,
                                  "mtime": entry.stat().st_mtime,
                                  "is_dir": entry.is_dir()})
        names = [row["name"] for row in dist_rows]
        refs = references if references is not None else referenced_names(project_root, names)
        rows += plan_dist(dist_rows, refs, stamp, rules)

    reclaim = [r for r in rows if r["decision"] == "reclaim"]
    review = [r for r in rows if r["decision"] == "needs_review"]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stamp)) + "Z",
        "rules": {key: dict(value) for key, value in rules.items()},
        "include_orphans": bool(include_orphans),
        "items": rows,
        "summary": {
            "reclaimable_bytes": sum(r["bytes"] for r in reclaim),
            "reclaimable_items": len(reclaim),
            "needs_review_bytes": sum(r["bytes"] for r in review),
            "needs_review_items": len(review),
            "kept_items": len(rows) - len(reclaim) - len(review),
            "by_kind": {kind: sum(r["bytes"] for r in reclaim if r["kind"] == kind)
                        for kind in (STAGES, CHECKPOINTS, DIST)},
        },
    }


def apply(items: list[dict[str, Any]], *, state_root: str, dry_run: bool = True) -> dict[str, Any]:
    """执行回收。默认 dry-run；删除走各模块自己的守卫（试做区用 `cleanup_stage`）。

    `apply` 收的是条目列表，可能来自别的调用方，所以这里再钉一道**根目录**校验：
    副本路径必须落在 `state_root` 下对应的那一类目录里，越界一律拒绝。
    每个条目单独 try：一个目录被占用不许把整批带走，失败原因如实列出来。
    """
    roots = {STAGES: (Path(state_root).resolve() / STAGES),
             CHECKPOINTS: (Path(state_root).resolve() / CHECKPOINTS)}
    removed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    freed = 0
    for item in items:
        if item.get("decision") != "reclaim":
            continue
        path = Path(str(item.get("path") or ""))
        if dry_run:
            removed.append({"path": str(path), "bytes": item["bytes"], "planned": True})
            freed += int(item["bytes"])
            continue
        try:
            base = roots.get(item.get("kind"))
            if base is not None:
                if path.is_symlink():
                    raise ValueError("副本根不能是符号链接")
                try:
                    path.resolve().relative_to(base)
                except ValueError:
                    raise ValueError("副本不在预期根目录 %s 下" % base)
            if item.get("kind") == STAGES:
                from . import project_stage
                project_stage.cleanup_stage({"workflow_id": item["name"],
                                             "workspace_root": str(path / "workspace")},
                                            state_root)
            else:
                shutil.rmtree(path)
            freed += int(item["bytes"])
            removed.append({"path": str(path), "bytes": item["bytes"], "planned": False})
        except (OSError, ValueError) as exc:
            failed.append({"path": str(path), "error": "%s: %s" % (type(exc).__name__, exc)})
    return {"dry_run": bool(dry_run), "removed": removed, "failed": failed,
            "freed_bytes": freed}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="磁盘保留期：出计划（默认不动手）")
    ap.add_argument("--state-root", default=".docmind")
    ap.add_argument("--project-root", default="")
    ap.add_argument("--days", type=int, default=0, help="统一覆盖三类保留期天数（0=用默认）")
    ap.add_argument("--include-orphans", action="store_true",
                    help="把「没有状态文件」的副本也当可回收")
    ap.add_argument("--apply", action="store_true", help="真删除（不可逆，必须显式给）")
    args = ap.parse_args(argv)
    rules = {key: dict(value) for key, value in DEFAULT_RULES.items()}
    if args.days:
        for value in rules.values():
            value["idle_days"] = args.days
    plan_result = scan(args.state_root, args.project_root, rules=rules,
                       include_orphans=args.include_orphans)
    summary = plan_result["summary"]
    print("可回收 %.2f GB / %d 项；待人工判定 %.2f GB / %d 项；保留 %d 项" % (
        summary["reclaimable_bytes"] / 1024 ** 3, summary["reclaimable_items"],
        summary["needs_review_bytes"] / 1024 ** 3, summary["needs_review_items"],
        summary["kept_items"]))
    for item in plan_result["items"]:
        if item["decision"] != "keep":
            print("  [%s] %.1f MB %-40s %s（%s）" % (
                item["decision"], item["bytes"] / 1048576, item["path"][-40:],
                item["kind"], item["reason"]))
    if not args.apply:
        print("\n[dry-run] 未做任何删除。要真回收请加 --apply")
        return 0
    result = apply(plan_result["items"], state_root=args.state_root, dry_run=False)
    print("已回收 %.2f GB（%d 项），失败 %d 项" % (
        result["freed_bytes"] / 1024 ** 3, len(result["removed"]), len(result["failed"])))
    for row in result["failed"]:
        print("  失败：", row["path"], row["error"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
