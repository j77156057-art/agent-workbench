"""Lane registry: exclusive file ownership for parallel agents on one repo.

多人同时改同一个仓库时，靠公告表和 markdown 交接来排他，会朝两个方向过期：任务表
显示 0% 而实现早已进树，或者两个会话同时领同一块。这里把「谁在动哪些文件」变成机器
可查的登记表（带 TTL 的租约）：认领冲突当场拒绝、过期自动可被接手、任何人想动某文件
都能先问一句「这块是谁的」。

状态存放在项目状态目录（不在 git 工作树内），worktree 是可选的开通动作而不是前提。
"""
from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import project_state
from . import process_runner

SCHEMA = 1
DEFAULT_TTL_MINUTES = 120
MIN_TTL_MINUTES = 5
MAX_TTL_MINUTES = 24 * 60
MAX_LANES = 200
MAX_PATHS = 40
_GIT_TIMEOUT = 60

#: `worktree add/remove` is the only write this module performs: it creates or
#: drops an extra checkout plus its branch. It never touches history, never
#: stages, never commits, never force-removes.
_GIT_ALLOWED = frozenset({"rev-parse", "status", "worktree"})

ACTIONS = ("claim", "status", "release", "heartbeat", "check", "open", "close")

_LANE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,48}$")
_WILDCARDS = "*?["
_LOCK_TIMEOUT_SECONDS = 3.0
_LOCK_STALE_SECONDS = 60.0


# --------------------------------------------------------------------------- #
# patterns
# --------------------------------------------------------------------------- #

def normalize_pattern(raw: Any) -> str:
    """Repo-relative, forward-slash pattern; a leading './' and quotes vanish."""
    text = str(raw or "").strip().strip("'\"").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def literal_prefix(pattern: str) -> str:
    """Longest wildcard-free directory prefix; '' means repo-wide.

    Conflict detection is deliberately conservative: two claims collide when
    either prefix contains the other. A basename pattern like ``*.vue`` has no
    directory prefix, so it really does reserve the whole repository — the tool
    says so instead of pretending it is narrow.
    """
    parts: list[str] = []
    for seg in pattern.split("/"):
        if any(ch in seg for ch in _WILDCARDS):
            break
        parts.append(seg)
    return "/".join(parts)


def _nested(prefix: str, other: str) -> bool:
    if not prefix or not other:
        return True
    return (prefix == other or other.startswith(prefix + "/")
            or prefix.startswith(other + "/"))


def pattern_matches(pattern: str, rel_path: Any) -> bool:
    """Does a claim pattern cover this concrete relative path?"""
    rel = str(rel_path or "").strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if not pattern or not rel:
        return False
    if not any(ch in pattern for ch in _WILDCARDS):
        return rel == pattern or rel.startswith(pattern + "/")
    if pattern.startswith("**/"):
        head = pattern[3:]
        return fnmatch.fnmatch(rel.rsplit("/", 1)[-1], head) or fnmatch.fnmatch(rel, head)
    if "/" in pattern:
        return fnmatch.fnmatch(rel, pattern.replace("**", "*"))
    return (fnmatch.fnmatch(rel, pattern)
            or fnmatch.fnmatch(rel.rsplit("/", 1)[-1], pattern))


def _clean_lane(lane: Any) -> tuple[str, str]:
    name = str(lane or "").strip().strip("'\"")
    if not name:
        return "", "缺少 lane：例如 AI-A、root、lane-3。"
    if not _LANE_NAME.match(name) or ".." in name:
        return "", f"拒绝 lane 名 {name!r}：只能用字母数字与 . _ -，长度 ≤49，且不能含 .."
    return name, ""


def _clean_scalar(value: Any, field: str, *, limit: int = 120) -> tuple[str, str]:
    text = str(value or "").strip().strip("'\"")
    if not text:
        return "", ""
    if text.startswith("-") or text.startswith("=") or any(ord(ch) < 32 for ch in text):
        return "", f"拒绝 {field}：取值 {text!r} 疑似参数注入或含控制字符。"
    if len(text) > limit:
        return "", f"拒绝 {field}：取值过长（上限 {limit} 字符）。"
    return text, ""


def _clean_ttl(raw: Any) -> tuple[float, str]:
    try:
        ttl = float(str(raw or "").strip() or DEFAULT_TTL_MINUTES)
    except (TypeError, ValueError):
        return 0.0, f"拒绝 ttl_minutes：不是数字（{raw!r}）。"
    if ttl <= 0:
        return 0.0, "拒绝 ttl_minutes：必须为正数。"
    return max(MIN_TTL_MINUTES, min(ttl, MAX_TTL_MINUTES)), ""


# --------------------------------------------------------------------------- #
# storage (cross-process safe)
# --------------------------------------------------------------------------- #

def _registry_path(root: str) -> Path:
    return Path(project_state.path(root, "lanes.json"))


def _lock_dir(root: str) -> Path:
    return _registry_path(root).parent / "lanes.lock"


def _read(root: str) -> dict[str, Any]:
    """Load the registry. A corrupt file is quarantined, never fatal."""
    target = _registry_path(root)
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {"schema": SCHEMA, "lanes": {}}
    try:
        data = json.loads(raw)
    except ValueError:
        try:
            target.replace(target.with_name(f"lanes.corrupt-{int(time.time())}.json"))
            note = "登记表损坏，已另存为 lanes.corrupt-*.json 并重建空表。"
        except OSError:
            note = "登记表损坏且无法另存，已按空表继续。"
        return {"schema": SCHEMA, "lanes": {}, "notes": [note]}
    if not isinstance(data, dict) or not isinstance(data.get("lanes"), dict):
        return {"schema": SCHEMA, "lanes": {}, "notes": ["登记表结构不认识，已按空表继续（原文件保留）。"]}
    return data


def _write(root: str, lanes: dict[str, Any]) -> None:
    target = _registry_path(root)
    tmp = target.with_name("lanes.json.tmp")
    payload = json.dumps({"schema": SCHEMA, "lanes": lanes},
                         ensure_ascii=False, indent=2, sort_keys=True)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)


def _acquire(lock: Path) -> None:
    """mkdir is atomic on Windows and POSIX, so the lock dir is a real mutex."""
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock.mkdir()
            return
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = _LOCK_STALE_SECONDS + 1
            if age > _LOCK_STALE_SECONDS:
                shutil.rmtree(lock, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("登记表正被另一个会话写入（>3s），稍后重试即可。")
            time.sleep(0.05)
        except FileNotFoundError:
            lock.parent.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def _locked(root: str) -> Iterator[None]:
    lock = _lock_dir(root)
    _acquire(lock)
    try:
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def _now(now: Any) -> float:
    return time.time() if now is None else float(now)


def _touched(row: dict[str, Any]) -> float:
    return float(row.get("renewed_at") or row.get("created_at") or 0)


def _seconds_left(row: dict[str, Any], now: float) -> float:
    ttl = float(row.get("ttl_minutes") or DEFAULT_TTL_MINUTES)
    return max(0.0, ttl * 60 - (now - _touched(row)))


def _is_live(row: dict[str, Any], now: float) -> bool:
    ttl = float(row.get("ttl_minutes") or DEFAULT_TTL_MINUTES)
    return (now - _touched(row)) <= ttl * 60


def _view(name: str, row: dict[str, Any], now: float) -> dict[str, Any]:
    return {
        "lane": name, "owner": str(row.get("owner") or ""),
        "paths": list(row.get("paths") or []),
        "ttl_minutes": float(row.get("ttl_minutes") or DEFAULT_TTL_MINUTES),
        "seconds_left": round(_seconds_left(row, now), 1),
        "live": _is_live(row, now),
        "note": str(row.get("note") or ""),
        "worktree": str(row.get("worktree") or ""),
        "branch": str(row.get("branch") or ""),
    }


# --------------------------------------------------------------------------- #
# registry actions
# --------------------------------------------------------------------------- #

def status(root: str, *, now: Any = None) -> dict[str, Any]:
    now = _now(now)
    with _locked(root):
        data = _read(root)
    live, expired = [], []
    for name in sorted(data["lanes"]):
        row = _view(name, data["lanes"][name] or {}, now)
        (live if row["live"] else expired).append(row)
    return {"ok": True, "action": "status", "live": live, "expired": expired,
            "count": len(live), "notes": list(data.get("notes") or [])}


def claim(root: str, lane: Any, paths: list[str], *, owner: Any = "",
          ttl_minutes: Any = "", note: Any = "", force: bool = False,
          now: Any = None) -> dict[str, Any]:
    """Reserve file patterns for a lane; overlapping live claims are rejected.

    Re-claiming your own lane replaces its scope (and renews the lease), so a
    lane that grows its footprint just calls claim again with the full list.
    """
    now = _now(now)
    name, err = _clean_lane(lane)
    if err:
        return {"ok": False, "action": "claim", "error": err, "conflicts": []}
    raw_paths = [normalize_pattern(p) for p in (paths or [])]
    cleaned: list[str] = []
    for p in raw_paths:
        if not p:
            continue
        if any(ord(ch) < 32 for ch in p) or p.startswith("-") or ".." in p.split("/"):
            return {"ok": False, "action": "claim", "error": f"拒绝 path 模式 {p!r}。",
                    "conflicts": []}
        cleaned.append(p)
    cleaned = list(dict.fromkeys(cleaned))
    if not cleaned:
        return {"ok": False, "action": "claim", "conflicts": [],
                "error": "缺少 paths：至少认领一个文件或目录，例如 paths: frontend/src/workbench/**"}
    if len(cleaned) > MAX_PATHS:
        return {"ok": False, "action": "claim", "conflicts": [],
                "error": f"拒绝：一次最多认领 {MAX_PATHS} 个模式（当前 {len(cleaned)}）"}
    owner_text, err = _clean_scalar(owner, "owner", limit=60)
    if err:
        return {"ok": False, "action": "claim", "error": err, "conflicts": []}
    owner_text = owner_text or name
    ttl, err = _clean_ttl(ttl_minutes)
    if err:
        return {"ok": False, "action": "claim", "error": err, "conflicts": []}
    note_text, err = _clean_scalar(note, "note", limit=120)
    if err:
        return {"ok": False, "action": "claim", "error": err, "conflicts": []}

    with _locked(root):
        data = _read(root)
        lanes = data["lanes"]
        conflicts: list[dict[str, Any]] = []
        warnings: list[str] = []
        for other_name, other_row in sorted(lanes.items()):
            if other_name == name or not _is_live(other_row or {}, now):
                continue
            row = other_row or {}
            for mine in cleaned:
                mine_prefix = literal_prefix(mine)
                for theirs in list(row.get("paths") or []):
                    if _nested(mine_prefix, literal_prefix(theirs)):
                        conflicts.append({"lane": other_name, "owner": str(row.get("owner") or ""),
                                          "pattern": theirs, "collides_with": mine})
        existing = lanes.get(name)
        same_owner = bool(existing) and str((existing or {}).get("owner") or "") == owner_text
        if conflicts and not force:
            return {"ok": False, "action": "claim", "lane": name,
                    "conflicts": conflicts,
                    "error": "这些范围已被其他在途 lane 认领；把问题清单发给对应 owner，"
                             "或缩小 paths，或确认无重复后加 force: true 强行共管。"}
        if len(lanes) >= MAX_LANES and not existing:
            return {"ok": False, "action": "claim", "conflicts": [],
                    "error": f"登记表已满（{MAX_LANES} 条）；先 release 或清理过期 lane。"}
        if conflicts and force:
            warnings.append("已 force 共管，冲突范围："
                            + "、".join(f"{c['lane']}:{c['pattern']}" for c in conflicts[:8]))
        if existing and _is_live(existing, now) and not same_owner:
            warnings.append(f"同名 lane 原本属于 {str(existing.get('owner') or '')}，"
                            f"本次由 {owner_text} 接管（原认领人请重新认领或改用其他 lane 名）。")
        created = float((existing or {}).get("created_at") or now)
        lanes[name] = {"owner": owner_text, "paths": cleaned, "ttl_minutes": ttl,
                       "created_at": created, "renewed_at": now, "note": note_text,
                       "worktree": str((existing or {}).get("worktree") or ""),
                       "branch": str((existing or {}).get("branch") or "")}
        _write(root, lanes)
        row = _view(name, lanes[name], now)
    if not warnings and row["seconds_left"] <= 0:
        warnings.append("TTL 立即过期，请加大 ttl_minutes。")
    return {"ok": True, "action": "claim", **row, "conflicts": conflicts,
            "warnings": warnings}


def heartbeat(root: str, lane: Any, *, owner: Any = "", now: Any = None) -> dict[str, Any]:
    now = _now(now)
    name, err = _clean_lane(lane)
    if err:
        return {"ok": False, "action": "heartbeat", "error": err}
    owner_text, err = _clean_scalar(owner, "owner", limit=60)
    if err:
        return {"ok": False, "action": "heartbeat", "error": err}
    with _locked(root):
        data = _read(root)
        row = data["lanes"].get(name)
        if not row:
            return {"ok": False, "action": "heartbeat",
                    "error": f"没有 {name} 的认领记录，先 claim。"}
        if owner_text and str(row.get("owner") or "") != owner_text:
            return {"ok": False, "action": "heartbeat",
                    "error": f"{name} 属于 {row.get('owner')}，不能替它续租。"}
        row["renewed_at"] = now
        _write(root, data["lanes"])
        view = _view(name, row, now)
    return {"ok": True, "action": "heartbeat", **view}


def release(root: str, lane: Any, *, owner: Any = "", now: Any = None) -> dict[str, Any]:
    now = _now(now)
    name, err = _clean_lane(lane)
    if err:
        return {"ok": False, "action": "release", "error": err}
    owner_text, err = _clean_scalar(owner, "owner", limit=60)
    if err:
        return {"ok": False, "action": "release", "error": err}
    with _locked(root):
        data = _read(root)
        row = data["lanes"].get(name)
        if not row:
            return {"ok": False, "action": "release", "error": f"没有 {name} 的认领记录。"}
        if owner_text and str(row.get("owner") or "") != owner_text:
            return {"ok": False, "action": "release",
                    "error": f"{name} 属于 {row.get('owner')}，不能替它释放。"}
        worktree = str(row.get("worktree") or "")
        del data["lanes"][name]
        _write(root, data["lanes"])
    notes = []
    if worktree:
        notes.append(f"该 lane 的 worktree 仍留在 {worktree}，需要时用 close 收回（本工具不自动删除文件）。")
    return {"ok": True, "action": "release", "lane": name, "notes": notes}


def check(root: str, path: Any, *, exclude_lane: Any = "", now: Any = None) -> dict[str, Any]:
    """Who currently owns this concrete path?"""
    now = _now(now)
    rel = normalize_pattern(path)
    if not rel:
        return {"ok": False, "action": "check", "error": "缺少 path。", "owners": []}
    exclude, err = _clean_lane(exclude_lane or "")
    if exclude_lane and err:
        return {"ok": False, "action": "check", "error": err, "owners": []}
    with _locked(root):
        data = _read(root)
    owners = []
    for name in sorted(data["lanes"]):
        if name == exclude or not _is_live(data["lanes"][name] or {}, now):
            continue
        row = data["lanes"][name] or {}
        hit = [p for p in list(row.get("paths") or []) if pattern_matches(p, rel)]
        if hit:
            owners.append({"lane": name, "owner": str(row.get("owner") or ""),
                           "patterns": hit, "seconds_left": round(_seconds_left(row, now), 1)})
    return {"ok": True, "action": "check", "path": rel, "owners": owners,
            "free": not owners}


# --------------------------------------------------------------------------- #
# write gate (决策 7 第二层) + 写账本 (决策 17)
# --------------------------------------------------------------------------- #
#
# 设计要点（来自 docs/cockpit-v2-redesign.md §7 / 决策 5·7·17）：
#   · lanes 原本是「君子协定」——只有 dev_lanes 工具被 agent 主动调用，没有任何
#     写路径在落盘前反查归属。第二层把安全性从「靠自觉」变成「靠拦截」。
#   · 本进程内用 thread-local 记录「当前写入方属于哪个 lane」，子对话（小对话）
#     在各自线程里 set_current_lane 后落盘，互不串扰。
#   · **零开销透传**：当前写入方未绑定 lane（单用户 / 编排主代理）时，write_gate
#     直接放行、不碰登记表——绝不允许这道附加安全层打断正常的单用户写路径。
#   · 防御式：任何异常一律放行（宁可放过，不可误伤正常写）。
#   · 写账本只在「有 lane 上下文」时记录，单用户模式零 I/O。
# --------------------------------------------------------------------------- #

_lane_local = threading.local()


def set_current_lane(name: Any) -> None:
    """绑定当前线程的写入方到某个 lane（小对话开工时调用）。传空/None 解除绑定。"""
    cleaned = str(name or "").strip() if name is not None else ""
    _lane_local.name = cleaned or None


def get_current_lane() -> str:
    return getattr(_lane_local, "name", None) or ""


@contextlib.contextmanager
def lane_context(name: Any) -> Iterator[None]:
    """上下文管理器版：进入时绑定、退出时恢复，便于子对话一次性包裹。"""
    prev = get_current_lane()
    set_current_lane(name)
    try:
        yield
    finally:
        set_current_lane(prev)


def write_gate(root: str, rel_path: Any, *, lane: Any = None, now: Any = None) -> str | None:
    """落盘前反查 lane 归属；越界返回阻塞原因（中文），放行返回 None。

    仅在「写入方绑定了 lane」时才查登记表——单用户 / 编排主代理（无 lane 上下文）
    恒为 None（透传）。当文件被某个 live lane 认领、且当前写入方不在认领者之列时
    拦截（不落盘）。其余情况（free、自己认领的、无 lane 上下文）一律放行。

    防御式：任何异常返回 None（放行），绝不因登记表不可用而误伤正常写。
    """
    name = lane if lane is not None else get_current_lane()
    if not name:
        return None  # 零开销透传：无 lane 上下文
    try:
        rel = normalize_pattern(rel_path)
        if not rel:
            return None
        result = check(root, rel, exclude_lane=name, now=now)
        if not result.get("ok") or result.get("free"):
            return None
        owners = result.get("owners") or []
        if any(str(o.get("lane") or "") == str(name) for o in owners):
            return None  # 当前 lane 拥有该文件，放行
        owner_desc = "、".join(
            f"{o.get('lane')}({o.get('owner') or '未署名'})" for o in owners[:5])
        return (f"拒绝写入 {rel}：该文件已被 lane {owner_desc} 认领，"
                f"当前写入方 lane {name} 无权越界写入。"
                "请先 claim 该范围，或用 dev_lanes 协调归属后再写。")
    except Exception:  # noqa: BLE001 - 附加安全层故障必须 fail-open，不可误伤正常写
        return None


# ---- 写账本（决策 17 数据侧）-------------------------------------------- #

def _ledger_path(root: str) -> Path:
    return Path(project_state.path(root, "lane_writes.json"))


def _read_ledger(root: str) -> dict[str, Any]:
    target = _ledger_path(root)
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {"schema": SCHEMA, "writes": []}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"schema": SCHEMA, "writes": []}
    if not isinstance(data, dict) or not isinstance(data.get("writes"), list):
        return {"schema": SCHEMA, "writes": []}
    return data


def _write_ledger(root: str, data: dict[str, Any]) -> None:
    target = _ledger_path(root)
    tmp = target.with_name("lane_writes.json.tmp")
    payload = json.dumps({"schema": SCHEMA, "writes": data.get("writes", [])},
                         ensure_ascii=False, indent=2, sort_keys=True)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)


def record_write(root: str, lane: Any, path: Any, op: str,
                 before_sha256: str, *, now: Any = None) -> None:
    """决策 17：在写账本里记一笔「某 lane 实际写了什么」。

    仅当写入方绑定了 lane 才记录（单用户模式零 I/O）。lane 级回滚（决策 11/17）
    只有这里能拿到权威归属。best-effort：任何异常静默跳过，不拖累主写路径。
    """
    name = str(lane or "").strip() if lane is not None else ""
    if not name:
        return
    try:
        entry = {"lane": name, "path": normalize_pattern(path), "op": op,
                 "before_sha256": str(before_sha256 or ""),
                 "at": _now(now)}
        with _locked(root):
            data = _read_ledger(root)
            data.setdefault("writes", []).append(entry)
            _write_ledger(root, data)
    except Exception:  # noqa: BLE001
        return


def read_write_ledger(root: str, *, lane: Any = "") -> dict[str, Any]:
    """读取写账本；可按 lane 过滤。供 lane 级回滚（决策 17）消费。"""
    data = _read_ledger(root)
    writes = data.get("writes", [])
    name = str(lane or "").strip()
    if name:
        writes = [w for w in writes if str(w.get("lane") or "") == name]
    return {"ok": True, "count": len(writes), "writes": writes}


# --------------------------------------------------------------------------- #
# optional worktree provisioning
# --------------------------------------------------------------------------- #

def _git(root: str, args: list[str], *, timeout: int = _GIT_TIMEOUT) -> dict[str, Any]:
    if not args or args[0] not in _GIT_ALLOWED:
        return {"ok": False, "exit_code": None, "output": "",
                "error": f"拒绝执行：{args[0] if args else ''} 不在 lane 工具白名单内。"}
    argv = ["git", "--no-pager", "-c", "core.quotepath=false", *args]
    rep = process_runner.run_bounded(argv, cwd=root, timeout=timeout,
                                     command_text=" ".join(argv))
    return rep


def _repo_top(root: str) -> tuple[str, str]:
    rep = _git(root, ["rev-parse", "--show-toplevel"])
    if rep.get("error") or rep.get("exit_code") != 0:
        return "", "当前代码根目录不是 git 工作树，无法开通 lane worktree。"
    line = (rep.get("output") or "").strip().splitlines()
    return (os.path.normpath(line[0]) if line else "", "")


def _worktree_base(root: str) -> Path:
    return Path(project_state.directory(root)) / "worktrees"


def open_worktree(root: str, lane: Any, *, base_ref: Any = "",
                  now: Any = None, timeout: int = _GIT_TIMEOUT) -> dict[str, Any]:
    """Provision an isolated checkout for a claimed lane (never forced)."""
    now = _now(now)
    name, err = _clean_lane(lane)
    if err:
        return {"ok": False, "action": "open", "error": err}
    ref, err = _clean_scalar(base_ref, "base_ref", limit=80)
    if err:
        return {"ok": False, "action": "open", "error": err}
    if ref and ("{" in ref or "*" in ref or "~" in ref and "^" in ref):
        return {"ok": False, "action": "open", "error": f"拒绝 base_ref {ref!r}。"}
    top, err = _repo_top(root)
    if err:
        return {"ok": False, "action": "open", "error": err}
    with _locked(root):
        data = _read(root)
        row = data["lanes"].get(name)
        if not row:
            return {"ok": False, "action": "open",
                    "error": f"先 claim {name}，再开通 worktree。"}
        existing = str(row.get("worktree") or "")
        if existing and os.path.isdir(existing):
            return {"ok": True, "action": "open", "lane": name, "already": True,
                    "worktree": existing, "branch": str(row.get("branch") or ""),
                    "notes": ["worktree 已存在，直接用它干活。"]}
        branch = f"lane-{name}"
        target = _worktree_base(root) / name.replace("/", "-")
        if target.exists():
            return {"ok": False, "action": "open", "lane": name,
                    "error": f"目标目录已存在：{target}。先用 close 收回，或换个 lane 名。"}
        target.parent.mkdir(parents=True, exist_ok=True)
        if branch_exists(top, branch):
            args = ["worktree", "add", str(target), branch]
            row_branch = branch
        else:
            # 没有同名分支时 --detach：不凭空占用分支，避免与主工作树抢 checkout
            args = ["worktree", "add", "--detach", str(target), ref or "HEAD"]
            row_branch = ""
        rep = _git(top, args, timeout=timeout)
        if rep.get("error") or rep.get("exit_code") != 0:
            return {"ok": False, "action": "open", "lane": name,
                    "error": "git worktree add 失败："
                             + ((rep.get("output") or rep.get("error") or "").strip()[-300:] or "无输出")}
        row["worktree"] = os.path.normpath(str(target))
        row["branch"] = row_branch
        row["renewed_at"] = now
        _write(root, data["lanes"])
    return {"ok": True, "action": "open", "lane": name, "worktree": row["worktree"],
            "branch": row["branch"],
            "notes": ["把 run_command 的 cwd 与读写路径切到这个目录，别人就看不见你的半成品。"]}


def branch_exists(top: str, branch: str) -> bool:
    rep = _git(top, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    return bool(rep) and rep.get("exit_code") == 0


def close_worktree(root: str, lane: Any, *, discard: bool = False,
                   now: Any = None, timeout: int = _GIT_TIMEOUT) -> dict[str, Any]:
    """Remove a lane worktree. Refuses a dirty checkout unless discard is given."""
    now = _now(now)
    name, err = _clean_lane(lane)
    if err:
        return {"ok": False, "action": "close", "error": err}
    with _locked(root):
        data = _read(root)
        row = data["lanes"].get(name)
        if not row:
            return {"ok": False, "action": "close", "error": f"没有 {name} 的认领记录。"}
        worktree = str(row.get("worktree") or "")
        if not worktree:
            return {"ok": True, "action": "close", "lane": name,
                    "notes": ["该 lane 没有开通 worktree，无需收回。"]}
        if not os.path.isdir(worktree):
            row["worktree"] = ""
            _write(root, data["lanes"])
            return {"ok": True, "action": "close", "lane": name,
                    "notes": [f"{worktree} 已不存在，登记表里的路径已清除。"]}
        if not discard:
            dirty = _git(worktree, ["status", "--porcelain"], timeout=timeout)
            if (dirty.get("output") or "").strip():
                return {"ok": False, "action": "close", "lane": name, "worktree": worktree,
                        "error": "worktree 里还有未提交改动，不敢删。先在里面提交/搬走，"
                                 "或确认丢弃后加 discard: true 重试。"}
        args = ["worktree", "remove", worktree]
        if discard:
            args.insert(2, "--force")
        top, _ = _repo_top(root)
        rep = _git(top or root, args, timeout=timeout)
        if rep.get("error") or rep.get("exit_code") != 0:
            return {"ok": False, "action": "close", "lane": name,
                    "error": "git worktree remove 失败："
                             + ((rep.get("output") or rep.get("error") or "").strip()[-300:] or "无输出")}
        row["worktree"] = ""
        row["renewed_at"] = now
        _write(root, data["lanes"])
    return {"ok": True, "action": "close", "lane": name,
            "notes": ["worktree 已收回；lane 认领仍在登记表里，不需要时用 release 释放。"]}


# --------------------------------------------------------------------------- #
# rendering for the agent
# --------------------------------------------------------------------------- #

def _fmt_minutes(seconds: float) -> str:
    if seconds <= 0:
        return "已过期"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 60:.0f} 分钟"


def render(result: dict[str, Any]) -> str:
    action = result.get("action") or ""
    if not result.get("ok"):
        lines = [f"dev_lanes({action}) 未完成：" + (result.get("error") or "未知原因")]
        for item in result.get("conflicts") or []:
            lines.append(f"  冲突：{item['lane']}（owner {item['owner'] or '未署名'}）"
                         f"已认领 {item['pattern']}，与你的 {item['collides_with']} 重叠")
        return "\n".join(lines)
    if action == "status":
        live, expired = result.get("live") or [], result.get("expired") or []
        if not live and not expired:
            lines = ["登记表里没有在途 lane（空表）。"]
        else:
            lines = [f"在途 lane：{len(live)} 条"
                     + (f"，另有 {len(expired)} 条已过期可被接管" if expired else "")]
            for row in live:
                lines.append(_render_row(row))
            for row in expired:
                lines.append(_render_row(row) + "  ← 过期")
        for note in result.get("notes") or []:
            lines.append("提示: " + note)
        return "\n".join(lines)
    if action == "check":
        owners = result.get("owners") or []
        if not owners:
            return f"{result.get('path')}：当前无人认领，可以放心动。"
        lines = [f"{result.get('path')}：{len(owners)} 条在途认领"]
        for row in owners:
            lines.append(f"  {row['lane']}（owner {row['owner'] or '未署名'}）"
                         f"，匹配 {', '.join(row['patterns'])}，剩余 {_fmt_minutes(row['seconds_left'])}")
        lines.append("动别人的范围前先把问题清单发给 owner，或按 status 说明缩小范围。")
        return "\n".join(lines)
    if action in ("open", "close"):
        lines = []
        if action == "open":
            lines.append(("worktree 已就绪：" if not result.get("already") else "worktree 早已存在：")
                         + str(result.get("worktree")))
            if result.get("branch"):
                lines.append(f"分支：{result['branch']}")
        else:
            lines.append(f"lane {result.get('lane')} 的 worktree 已收回。")
        lines += ["提示: " + note for note in result.get("notes") or []]
        return "\n".join(lines)
    if action == "release":
        lines = [f"已释放 lane：{result.get('lane')}"]
        lines += ["提示: " + note for note in result.get("notes") or []]
        return "\n".join(lines)
    row = {key: result.get(key) for key in
           ("lane", "owner", "paths", "seconds_left", "worktree", "branch", "note")}
    row["seconds_left"] = float(row.get("seconds_left") or 0)
    head = {"claim": "认领成功", "heartbeat": "已续租", "release": "已释放"}.get(action, action)
    lines = [head + "：" + _render_row(row)]
    lines += ["提示: " + note for note in (result.get("warnings") or result.get("notes") or [])]
    return "\n".join(lines)


def _render_row(row: dict[str, Any]) -> str:
    parts = [f"{row['lane']}（owner {row['owner'] or '未署名'}）"]
    parts.append("剩余 " + _fmt_minutes(float(row.get("seconds_left") or 0)))
    parts.append("范围 " + (", ".join(row.get("paths") or []) or "无"))
    if row.get("worktree"):
        parts.append("worktree " + row["worktree"])
    if row.get("note"):
        parts.append("备注 " + row["note"])
    return "  ".join(parts)
