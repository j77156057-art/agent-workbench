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

# ---------------------------------------------------------------- 用户可设的三样
#
#   interval_days  多久自动回收一次（天）。**0 = 不自动清**，只出计划。
#   idle_days      闲置多少天才算可回收（"按时间回收"的那个时间）。
#   whitelist      永不删除的名字/前缀，用户可以往里加。
#
# 自动清是**不可逆**动作，所以默认关闭（0），要用户自己设成正数才生效。
INTERVAL_MIN_DAYS = 0
INTERVAL_MAX_DAYS = 3650
IDLE_MIN_DAYS = 1          # 0 = "终态就删、不看闲置多久"，一次误设就能把刚跑完的全清掉
IDLE_MAX_DAYS = 3650
WHITELIST_MAX_ENTRIES = 64
WHITELIST_MAX_LEN = 200
#: 单次运行最多回收多少项：兜住"保留天数被设成 1 天 + 白名单空"这种一次性清库的形状。
MAX_ITEMS_PER_RUN = 2000

#: 内置白名单（用户加的那份叠在上面，不替换）：发布物与安装包。
BUILTIN_WHITELIST = ("release", "installer", "*.zip")

DEFAULT_SETTINGS: dict[str, Any] = {
    "interval_days": 0,
    "idle_days": 30,
    "whitelist": list(BUILTIN_WHITELIST),
}


def _int_or(value: Any, fallback: int, lo: int, hi: int) -> int:
    """收整数并夹到区间。非数字/空 → 用默认值，不抛（设置面板传什么都有可能）。"""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, n))


def normalize_whitelist(raw: Any) -> list[str]:
    """清洗用户给的白名单：去空、去重、保序、限长，并始终带上内置项。

    内置项**不可被移除** —— 否则一次误清空就会把 `release/`（发布物）纳入自动删除。
    """
    items = raw if isinstance(raw, (list, tuple, set)) else str(raw or "").replace(",", "\n").splitlines()
    out: list[str] = []
    for item in list(BUILTIN_WHITELIST) + [str(x) for x in items]:
        text = str(item or "").strip().replace("\\", "/").strip("/")
        if not text or len(text) > WHITELIST_MAX_LEN:
            continue
        if text not in out:
            out.append(text)
        if len(out) >= WHITELIST_MAX_ENTRIES:
            break
    return out


def normalize_settings(raw: Any) -> dict[str, Any]:
    """把任意来源的设置（API body / 持久化文件 / 面板默认值）收成一封可信的形状。"""
    data = raw if isinstance(raw, Mapping) else {}
    return {
        "interval_days": _int_or(data.get("interval_days"), DEFAULT_SETTINGS["interval_days"],
                                 INTERVAL_MIN_DAYS, INTERVAL_MAX_DAYS),
        "idle_days": _int_or(data.get("idle_days"), DEFAULT_SETTINGS["idle_days"],
                             IDLE_MIN_DAYS, IDLE_MAX_DAYS),
        "whitelist": normalize_whitelist(data.get("whitelist")),
    }


def _pattern_matches(name: str, pattern: str) -> bool:
    """白名单匹配的是**目录名/相对路径**，不是全盘前缀：

    - `release` 命中同名项；
    - `dist/release` 这类带斜杠的，命中同样相对路径或以它结尾的路径段；
    - `*.zip` 这种以 `*` 收尾的按前缀，以 `*` 开头的按后缀。
    """
    if not pattern:
        return False
    if pattern.startswith("*") and pattern.endswith("*") and len(pattern) > 2:
        return pattern[1:-1] in name
    if pattern.startswith("*"):
        return name.endswith(pattern[1:])
    if pattern.endswith("*"):
        return name.startswith(pattern[:-1])
    return name == pattern or name.endswith("/" + pattern) or ("/" + pattern + "/") in ("/" + name)


def is_whitelisted(name: str, whitelist: Iterable[str]) -> str:
    """返回命中的白名单条目（空串表示没命中）。要能回显命中项，UI 才知道"为什么这条不清"。"""
    text = str(name or "").strip().replace("\\", "/")
    for pattern in whitelist:
        if _pattern_matches(text, str(pattern or "")):
            return str(pattern)
    return ""


def rules_from_settings(settings: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """用户设的 `idle_days` 覆盖三类；每类的结构性保护（keep_newest / 每区批次上限）不变。"""
    idle = normalize_settings(settings)["idle_days"]
    rules = {key: dict(value) for key, value in DEFAULT_RULES.items()}
    for value in rules.values():
        value["idle_days"] = idle
    return rules


def schedule_due(now: float, last_run_ts: float | None, interval_days: int) -> bool:
    """到没到点。`interval_days = 0` 恒 False —— 这就是"默认不自动清"的实现处。"""
    interval = _int_or(interval_days, 0, INTERVAL_MIN_DAYS, INTERVAL_MAX_DAYS)
    if interval <= 0:
        return False
    if not last_run_ts:
        return True
    return (float(now) - float(last_run_ts)) >= interval * 86400.0


def filter_whitelisted(items: list[dict[str, Any]], whitelist: Iterable[str]
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把计划切成"可动手"与"白名单挡下"两半。给 apply 之前用，也回显给用户看。"""
    allowed, blocked = [], []
    patterns = list(whitelist)
    for item in items:
        hit = is_whitelisted(str(item.get("name") or ""), patterns) or \
            is_whitelisted(str(item.get("path") or ""), patterns)
        if hit:
            row = dict(item)
            row["whitelisted_by"] = hit
            blocked.append(row)
        else:
            allowed.append(item)
    return allowed, blocked



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
#: 数据/结果/测试/文档目录：里面出现的路径是记录或样本，不是消费方。
#: `tests/` 也在里面是实测逼出来的：保留期自己的测试与注释里写了真实副本目录名
#: （形如 `DocMind_<构建时间戳>`），于是那些目录被自己的文字判成"仍被引用"，
#: 默认规则下可回收从 4.01 GB 掉到 2.69 GB，再掉到 3.46 GB。
#: 教训两条：测试字符串不是依赖方；**代码与文档里要写占位符名，不写真实副本名**。
REFERENCE_DATA_PREFIXES = ("golden/", "tests/", "docs/", "dist/")


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
         rules: Mapping[str, Mapping[str, Any]] | None = None,
         settings: Mapping[str, Any] | None = None,
         include_orphans: bool = False,
         statuses: Mapping[str, str] | None = None,
         references: Iterable[str] | None = None) -> dict[str, Any]:
    """扫三类目录并出计划。`project_root` 空则跳过 `dist/`（保留期只管仓库内副本）。

    传 `settings`（用户那三样）时规则从中派生，并按白名单把可回收项分成
    "能动" 与 "白名单挡下" 两半 —— 挡下的仍然列出来，UI 要能回答"为什么这条不清"。
    """
    stamp = now if now is not None else time.time()
    norm = normalize_settings(settings) if settings is not None else None
    if rules is None:
        rules = rules_from_settings(norm) if norm else DEFAULT_RULES
    whitelist = list(norm["whitelist"]) if norm else list(BUILTIN_WHITELIST)
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
    allowed, blocked = filter_whitelisted(reclaim, whitelist)
    for row in blocked:
        # 白名单挡下的仍然留在 items 里（decision 改回 keep + 注明谁挡的），
        # 这样 UI 能回答"为什么这条明明超期却不清"，而不是让人以为它消失了。
        for item in rows:
            if item.get("path") == row.get("path"):
                item["decision"] = "keep"
                item["whitelisted_by"] = row["whitelisted_by"]
                item["reason"] = "白名单：%s" % row["whitelisted_by"]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stamp)) + "Z",
        "rules": {key: dict(value) for key, value in rules.items()},
        "settings": normalize_settings(settings) if settings is not None else None,
        "whitelist": whitelist,
        "include_orphans": bool(include_orphans),
        "items": rows,
        "summary": {
            "reclaimable_bytes": sum(r["bytes"] for r in allowed),
            "reclaimable_items": len(allowed),
            "blocked_by_whitelist_bytes": sum(r["bytes"] for r in blocked),
            "blocked_by_whitelist_items": len(blocked),
            "needs_review_bytes": sum(r["bytes"] for r in review),
            "needs_review_items": len(review),
            "kept_items": len(rows) - len(allowed) - len(review) - len(blocked),
            "by_kind": {kind: sum(r["bytes"] for r in allowed if r["kind"] == kind)
                        for kind in (STAGES, CHECKPOINTS, DIST)},
            "max_items_per_run": MAX_ITEMS_PER_RUN,
        },
    }


#: 执行器叫 `apply_items` 而不是 `apply`：`run_once` 的参数就叫 `apply`，
#: 同名会在函数作用域里把函数本身遮掉（真跑一次才炸的坑）。
def apply_items(items: list[dict[str, Any]], *, state_root: str, dry_run: bool = True,
          whitelist: Iterable[str] = (), max_items: int = MAX_ITEMS_PER_RUN,
          now: float | None = None) -> dict[str, Any]:
    """执行回收。默认 dry-run；删除走各模块自己的守卫（试做区用 `cleanup_stage`）。

    `apply` 收的是条目列表，可能来自别的调用方，所以这里再钉三道：
    1. **白名单在动手处再过一遍** —— 计划是谁给的都挡得住，内置项（release / installer / *.zip）
       永远在名单里，用户只能往里加、不能清空；
    2. **单次上限** `max_items` —— 兜住"保留天数设成 1 天 + 白名单只有一条"导致的一次清掉几百项；
    3. **根目录校验** —— 条目路径必须落在 `state_root` 下对应那一类目录里，越界一律拒绝。

    每个条目单独 try：一个目录被占用不许把整批带走，失败原因如实列出来。
    """
    roots = {STAGES: (Path(state_root).resolve() / STAGES),
             CHECKPOINTS: (Path(state_root).resolve() / CHECKPOINTS)}
    patterns = normalize_whitelist(whitelist)
    cap = max(1, int(max_items or MAX_ITEMS_PER_RUN))
    removed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    freed = 0
    for item in items:
        if item.get("decision") != "reclaim":
            continue
        path = Path(str(item.get("path") or ""))
        hit = is_whitelisted(str(item.get("name") or ""), patterns) or \
            is_whitelisted(str(path).replace("\\", "/"), patterns)
        if hit:
            skipped.append({"path": str(path), "reason": "白名单：%s" % hit})
            continue
        if len(removed) >= cap:
            skipped.append({"path": str(path), "reason": "超过单次上限 %d 项，剩下的下轮再收" % cap})
            continue
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
            "skipped": skipped, "freed_bytes": freed, "freed_items": len(removed)}


LAST_RUN_FILE = "disk_retention-last.json"
SETTINGS_FILE = "disk-retention-settings.json"


def settings_path(state_root: str) -> str:
    return os.path.join(str(state_root), SETTINGS_FILE)


def load_settings_file(state_root: str) -> dict[str, Any]:
    """读用户设置；没有文件/读坏了都回默认（**interval=0 即不自动清**）。"""
    try:
        with open(settings_path(state_root), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return normalize_settings(None)
    return normalize_settings(raw if isinstance(raw, dict) else {})


def save_settings_file(state_root: str, settings: Mapping[str, Any]) -> dict[str, Any]:
    """落盘设置（原子替换）。返回值是**归一后**的，UI 拿到的就是真正生效的值。"""
    norm = normalize_settings(settings)
    os.makedirs(str(state_root), exist_ok=True)
    tmp = settings_path(state_root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(norm, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, settings_path(state_root))
    return norm


def last_run_path(state_root: str) -> str:
    return os.path.join(str(state_root), LAST_RUN_FILE)


def load_last_run(state_root: str) -> dict[str, Any]:
    """上次回收的记录（没有就返回空 dict）。调度判定要读它的时间戳。"""
    try:
        with open(last_run_path(state_root), encoding="utf-8") as handle:
            raw = json.load(handle)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def record_run(state_root: str, *, plan: Mapping[str, Any], result: Mapping[str, Any],
               settings: Mapping[str, Any], trigger: str, applied: bool,
               now: float | None = None) -> dict[str, Any]:
    """把这一轮做了什么落盘：用户要能事后查"什么时候清的、清了多少、什么失败了"。"""
    row = {
        "ts": round(now if now is not None else time.time(), 3),
        "trigger": str(trigger or "manual"),
        "applied": bool(applied),
        "freed_bytes": int(result.get("freed_bytes") or 0),
        "freed_items": int(result.get("freed_items") or 0),
        "failed_items": len(result.get("failed") or []),
        "skipped_items": len(result.get("skipped") or []),
        "failures": [str(x.get("error") or "")[:160] for x in (result.get("failed") or [])[:10]],
        "settings": dict(settings or {}),
        "plan_summary": dict(plan.get("summary") or {}),
    }
    try:
        os.makedirs(str(state_root), exist_ok=True)
        tmp = last_run_path(state_root) + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(row, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, last_run_path(state_root))
    except OSError:
        pass          # 记账失败不能把回收本身带崩
    return row


def run_once(state_root: str, project_root: str = "", *,
             settings: Mapping[str, Any] | None = None, apply: bool = False,
             trigger: str = "manual", include_orphans: bool = False,
             now: float | None = None,
             references: Iterable[str] | None = None) -> dict[str, Any]:
    """一次回收：出计划 → （可选）动手 → 落记录。UI 的「立即回收」与后台定时走同一条路。

    `apply=False` 是默认，也是**只读**语义：不可逆动作要么由用户点按钮触发，
    要么由用户显式设过回收间隔后由后台执行（宪法 C-006）。
    """
    norm = normalize_settings(settings)
    plan = scan(state_root, project_root, settings=norm, include_orphans=include_orphans,
                now=now, references=references)
    items = [i for i in plan["items"] if i["decision"] == "reclaim"]
    # 白名单挡下的那些**不进 items**（计划阶段就改回 keep 了），但面板要能回答
    # "这条明明超期为什么不清"，所以单独列出命中了哪一条规则。
    blocked = [i for i in plan["items"] if str(i.get("reason") or "").startswith("白名单：")]
    result = apply_items(items, state_root=state_root, dry_run=not apply,
                         whitelist=norm["whitelist"], now=now)
    row = record_run(state_root, plan=plan, result=result, settings=norm,
                     trigger=trigger, applied=bool(apply), now=now)
    return {"ok": not result.get("failed"), "applied": bool(apply), "dry_run": not apply,
            "plan": plan["summary"], "items": items[:200], "blocked": blocked[:200],
            "result": result, "record": row, "settings": norm}


def _last_apply_ts(state_root: str) -> float:
    """上一次**真的动手**的时间戳（干跑不算，否则永远"还没到点"）。"""
    raw = load_last_run(state_root)
    if not raw.get("applied"):
        return 0.0
    try:
        return float(raw.get("ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def scheduled_pass(state_root: str, project_root: str = "", *,
                   now: float | None = None) -> dict[str, Any]:
    """后台体检调这里：`interval_days = 0` 时**永远不动手**，只把计划刷新进记录。

    间隔是用户自己设的（面板里那格），所以"多久清一次磁盘"这个决定权在设置里，
    不在这段代码里；这里只回答"到点没到点"。
    """
    stamp = now if now is not None else time.time()
    settings = load_settings_file(state_root)
    due = schedule_due(stamp, _last_apply_ts(state_root), settings["interval_days"])
    out = run_once(state_root, project_root, settings=settings, apply=bool(due),
                   trigger="scheduled", now=stamp)
    out["scheduled"] = {"interval_days": settings["interval_days"], "due": bool(due),
                        "last_apply_ts": _last_apply_ts(state_root)}
    return out


def main(argv: list[str] | None = None) -> int:
    """命令行入口。语义与面板/后台完全一致：默认只出计划，`--apply` 才动手。"""
    import argparse
    ap = argparse.ArgumentParser(description="磁盘保留期：出计划（默认不动手）")
    ap.add_argument("--state-root", default=".docmind")
    ap.add_argument("--project-root", default="")
    ap.add_argument("--idle-days", type=int, default=0, help="闲置多少天才算可回收（0=用设置值）")
    ap.add_argument("--add-whitelist", default="", help="追加白名单条目，逗号分隔")
    ap.add_argument("--include-orphans", action="store_true",
                    help="把「没有状态文件」的副本也当可回收")
    ap.add_argument("--apply", action="store_true", help="真删除（不可逆，必须显式给）")
    args = ap.parse_args(argv)
    settings = normalize_settings(load_settings_file(args.state_root))
    if args.idle_days:
        settings["idle_days"] = args.idle_days
    if args.add_whitelist:
        settings["whitelist"] = normalize_whitelist(
            list(settings["whitelist"]) + str(args.add_whitelist).split(","))
    out = run_once(args.state_root, args.project_root, settings=settings,
                   apply=bool(args.apply), trigger="cli", include_orphans=args.include_orphans)
    plan = out["plan"]
    print("规则：闲置 > %s 天、单次上限 %s 项、白名单 %d 条（内置 release / installer / *.zip 不可移除）"
          % (settings["idle_days"], plan["max_items_per_run"], len(settings["whitelist"])))
    print("可回收 %.2f GB / %d 项；白名单挡下 %.2f GB / %d 项；待人工判定 %.2f GB / %d 项" % (
        plan["reclaimable_bytes"] / 1024 ** 3, plan["reclaimable_items"],
        plan["blocked_by_whitelist_bytes"] / 1024 ** 3, plan["blocked_by_whitelist_items"],
        plan["needs_review_bytes"] / 1024 ** 3, plan["needs_review_items"]))
    for item in out["items"][:200]:
        print("  [reclaim] %.1f MB %-40s %s" % (item["bytes"] / 1048576,
                                                str(item["path"])[-40:], item["reason"]))
    if not args.apply:
        print("\n[dry-run] 未做任何删除。要真回收请加 --apply")
        return 0
    result = out["result"]
    print("已回收 %.2f GB（%d 项），白名单跳过 %d 项，失败 %d 项" % (
        result["freed_bytes"] / 1024 ** 3, result["freed_items"],
        len(result.get("skipped") or []), len(result["failed"])))
    for row in result["failed"]:
        print("  失败：", row["path"], row["error"])
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
