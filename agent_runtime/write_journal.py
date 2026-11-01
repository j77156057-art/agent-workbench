"""回合级撤销的底座：每次项目文件写入留一份「改前内容」，按回合反序恢复。

## 为什么不复用 lanes 的写账本

`lanes.record_write` 那张账本是 `{"writes": [...]}` **整本 JSON 重写**
（`_write_ledger`），每写一次的代价是 O(n)；而且它**只在绑了 lane 时才记**
（注释写明"单用户模式零 I/O"）—— 撤销需要的恰恰是"单人日常也要能退"。
所以这里另起一条 append-only JSONL + 内容寻址 blob：

- `lanes` 账本管**归属与冲突**（谁在动哪个文件）；
- 本模块管**恢复**（改之前是什么内容）。

两者都记 path/op，但回答的是不同问题，合并只会把 O(n) 重写带给每一次写。

## 撤销记录本身，但不自动反向

`undo()` 每恢复一个文件都会记一笔 `op="undo"`，把"撤销前那一刻的内容"也存成 blob ——
数据不会丢。但工具**不会**自动再退一次：实测过把补偿记录当原始写入重放，同一批文件
被来回搬两次，第二次撤销的结果和第一次一样，那不是"退回来"。要正向恢复得显式按账本
取 blob，这留给以后的 `redo`。
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from typing import Any, Iterator

import project_state

SCHEMA = 1
#: 单个改前内容超过这个字节就不存了：撤销是给人兜底的，不是给仓库做备份。
#: 真需要版本管理的东西本来就在 git 里。
MAX_BLOB_BYTES = 1024 * 1024
#: 账本行数与天数双上限；超出即截断（今天 12.5 GB 的教训：复制了却没保留期）。
KEEP_TURNS = 200
MAX_AGE_DAYS = 7
_LOCK = threading.RLock()

#: 本回合标识。**必须是 contextvar 而不是 threading.local**：并行工具派发走
#: `copy_context()`，threading.local 在工作线程里读不到，那一条批次里的写入就会
#: 记成"没有回合"，撤销时整批漏掉。
_turn_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "docmind_journal_turn", default="")


def bind_turn(turn_id: str) -> Any:
    """绑定本回合的 trace turn id，返回 reset 令牌。"""
    return _turn_var.set(str(turn_id or ""))


def reset_turn(token: Any) -> None:
    if token is None:
        return
    try:
        _turn_var.reset(token)
    except (ValueError, LookupError):  # 令牌不属于本上下文
        _turn_var.set("")


def current_turn() -> str:
    return _turn_var.get() or ""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value or b"").hexdigest()


def _dir(root: str) -> str:
    return os.path.dirname(project_state.path(root, "write_journal/ledger.jsonl"))


def _ledger(root: str) -> str:
    return project_state.path(root, "write_journal/ledger.jsonl")


def _blob_path(root: str, digest: str) -> str:
    return project_state.path(root, "write_journal/blobs/%s/%s" % (digest[:2], digest))


def _store_blob(root: str, value: bytes) -> str:
    """内容寻址存一份改前内容，返回 sha256；存不下（超限/二进制）返回空串。"""
    if not value or len(value) > MAX_BLOB_BYTES or b"\x00" in value[:8000]:
        return ""
    digest = sha256_bytes(value)
    target = _blob_path(root, digest)
    if os.path.isfile(target):
        return digest
    os.makedirs(os.path.dirname(target), exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(value)
        os.replace(tmp, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        return ""
    return digest


def read_blob(root: str, digest: str) -> bytes | None:
    """按 sha256 取回存下来的内容字节；没存/被裁掉/指纹不符返回 None（不抛）。"""
    digest = str(digest or "").lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return None                      # 账本里的指纹可能被手改过：不照它拼路径
    target = _blob_path(root, digest)
    try:
        with open(target, "rb") as handle:
            value = handle.read()
    except OSError:
        return None
    return value if sha256_bytes(value) == digest else None


def _read_lines(root: str) -> list[dict[str, Any]]:
    target = _ledger(root)
    rows = []
    try:
        with open(target, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return []
    return rows


def record(root: str, rel: str, *, op: str, before: bytes | None,
           after: bytes | None, lane: str = "", turn: str = "") -> dict[str, Any]:
    """记一笔写入。`before=None` 表示"改前内容拿不到"（撤销时会如实说恢复不了）。

    `after` 也存一份 blob：改前内容只能把文件退回**上一个**状态，
    「最新内容」此前在账本里只有一个 sha256 指纹、没有字节 —— 文件一旦被删
    （含被账本之外的手段删掉），那份内容在这个系统里就不存在了。
    2026-11-01 真实丢档就是这么发生的，事后确认无法逐字节恢复。

    代价：blob 存储量最坏翻倍。用两处压住：内容寻址（同内容只存一份）
    + `prune()` 的回合数/天数保留期。

    best-effort：任何 I/O 失败都不许把主写路径带崩 —— 记账丢了只是这一笔退不了，
    抛异常则整条改动都白做。
    """
    entry = {
        "schema": SCHEMA,
        "ts": round(time.time(), 3),
        "lane": str(lane or ""),
        "turn": str(turn or current_turn() or ""),
        "path": str(rel or "").replace("\\", "/").lstrip("/"),
        "op": str(op or "modify"),
        "after_sha": sha256_bytes(after or b""),
        "before_sha": "",
        "after_restorable": False,
        "restorable": False,
    }
    if op == "create":
        entry["restorable"] = True          # 撤销新建 = 删掉它，不需要旧内容
    elif before is not None:
        digest = _store_blob(root, before)
        if digest:
            entry["before_sha"] = digest
            entry["restorable"] = True
    if after:
        entry["after_restorable"] = _store_blob(root, after) == entry["after_sha"]
    with _LOCK:
        try:
            os.makedirs(os.path.dirname(_ledger(root)), exist_ok=True)
            with open(_ledger(root), "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            return entry
    return entry


def entries_for(root: str, *, lane: str = "", turn: str = "",
                limit: int = 2000) -> list[dict[str, Any]]:
    rows = _read_lines(root)[-int(limit):]
    if lane:
        rows = [row for row in rows if str(row.get("lane") or "") == str(lane)]
    if turn:
        rows = [row for row in rows if str(row.get("turn") or "") == str(turn)]
    return rows


def last_turn(root: str, *, lane: str = "") -> str:
    """最近一个有内容的回合 id（空 lane 时按"最后一次写入所属回合"取）。"""
    rows = entries_for(root, lane=lane)
    for row in reversed(rows):
        if str(row.get("turn") or ""):
            return str(row["turn"])
    return ""


def _restore_target(root: str, rel: str) -> str | None:
    """把账本里的相对路径解析回项目内绝对路径；越界返回 None（绝不越界写）。"""
    base = os.path.realpath(root)
    target = os.path.realpath(os.path.join(base, rel))
    if target == base or not target.startswith(base + os.sep):
        return None
    return target


@contextlib.contextmanager
def _locked(root: str) -> Iterator[None]:
    """跨进程互斥：与 lanes 同一套目录锁做法，避免两个 agent 同时撤销互相踩。"""
    lock = os.path.join(_dir(root), "undo.lock")
    deadline = time.time() + 20
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    acquired = False
    while time.time() < deadline:
        try:
            os.mkdir(lock)
            acquired = True
            break
        except FileExistsError:
            time.sleep(0.1)
        except OSError:
            break
    if not acquired:
        raise TimeoutError("撤销锁获取超时（有别的撤销在跑）")
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(lock, ignore_errors=True)


def undo_turn(root: str, *, turn: str = "", lane: str = "",
              dry_run: bool = False) -> dict[str, Any]:
    """反序恢复某回合的全部**原始写入**。

    四条硬规矩：
    1. **当前内容必须等于账本记的 after_sha**，否则说明这之后别人又改过 —— 跳过并
       如实报冲突，绝不覆盖（和 `apply_edit` 的"并行修改冲突"同一个口径）。
    2. 没存到改前内容的（超限/二进制/写入时读不到旧字节）报"不可恢复"，不假装成功。
    3. 补偿记录（`op=undo/undo-create/rollback`）**不参与重放**，同一回合也不许退第二次：
       实测过把补偿记录当原始写入重放，结果改动被来回搬，第二次撤销把文件又退回了
       撤销后的状态 —— 那不是"退回来"，那是把同一文件搬两次。
    4. 撤销前的内容仍然存成 blob 并记了账，所以数据不会丢；但要"redo"得显式按账本
       取 blob 恢复，工具不提供自动反向。
    """
    turn = str(turn or "") or last_turn(root, lane=lane)
    if not turn:
        return {"ok": False, "turn": "", "restored": [], "skipped": [],
                "error": "账本里没有可撤销的回合记录"}
    rows = [row for row in entries_for(root, turn=turn, lane=lane) if row.get("path")]
    already = [row for row in rows if str(row.get("op") or "").startswith("undo")]
    if already and not dry_run:
        return {"ok": False, "turn": turn, "restored": [], "skipped": [],
                "error": "这个回合已经撤销过一次了（再退一次会把同一批文件来回搬）"}
    original = [row for row in rows if not str(row.get("op") or "").startswith("undo")
                and str(row.get("op") or "") != "rollback"]
    restored, skipped = [], []
    with _locked(root):
        for row in reversed(original):
            rel = str(row.get("path") or "")
            target = _restore_target(root, rel)
            if not target or not os.path.isfile(target):
                skipped.append({"path": rel, "reason": "文件已不在项目内"})
                continue
            current = _read_bytes(target)
            if current is None:
                skipped.append({"path": rel, "reason": "读取失败"})
                continue
            if sha256_bytes(current) != str(row.get("after_sha") or ""):
                skipped.append({"path": rel, "reason": "该文件在此回合之后又被改过，已跳过（不覆盖别人的改动）"})
                continue
            if str(row.get("op") or "") == "create":
                if dry_run:
                    restored.append({"path": rel, "action": "将删除（本回合新建）", "bytes": len(current)})
                    continue
                _store_blob(root, current)
                try:
                    os.remove(target)
                except OSError as exc:
                    skipped.append({"path": rel, "reason": f"删除失败：{type(exc).__name__}"})
                    continue
                record(root, rel, op="undo-create", before=current, after=b"",
                       lane=str(row.get("lane") or ""), turn=turn)
                restored.append({"path": rel, "action": "已删除（本回合新建）", "bytes": len(current)})
                continue
            if not row.get("restorable") or not row.get("before_sha"):
                skipped.append({"path": rel, "reason": "改前内容没存下（超 1MB 上限或二进制），这一条恢复不了"})
                continue
            blob = _blob_path(root, str(row["before_sha"]))
            content = _read_bytes(blob)
            if content is None:
                skipped.append({"path": rel, "reason": "改前内容已随保留期清理掉了"})
                continue
            if dry_run:
                restored.append({"path": rel, "action": "将恢复", "bytes": len(content)})
                continue
            try:
                _atomic_write(target, content)
            except OSError as exc:
                skipped.append({"path": rel, "reason": f"写入失败：{type(exc).__name__}"})
                continue
            record(root, rel, op="undo", before=current, after=content,
                   lane=str(row.get("lane") or ""), turn=turn)
            restored.append({"path": rel, "action": "已恢复", "bytes": len(content)})
    return {"ok": not skipped, "turn": turn, "restored": restored, "skipped": skipped,
            "dry_run": bool(dry_run)}


def _read_bytes(target: str) -> bytes | None:
    try:
        with open(target, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _atomic_write(target: str, content: bytes) -> None:
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".undo.tmp")
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(content)
        os.replace(tmp, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def summarize(root: str, *, limit: int = 400) -> dict[str, Any]:
    """给面板/工具看的一页概览（不返回整本账）。"""
    rows = entries_for(root, limit=limit)
    by_turn: dict[str, int] = {}
    for row in rows:
        key = str(row.get("turn") or row.get("lane") or "-")
        by_turn[key] = by_turn.get(key, 0) + 1
    return {"writes": len(rows), "turns": len(by_turn),
            "restorable": sum(1 for row in rows if row.get("restorable")),
            "last_turn": rows[-1].get("turn", "") if rows else "",
            "recent": [{"turn": row.get("turn"), "path": row.get("path"),
                        "op": row.get("op"), "restorable": row.get("restorable")}
                       for row in rows[-8:]]}


def prune(root: str, *, keep_turns: int = KEEP_TURNS,
          max_age_days: int = MAX_AGE_DAYS, apply: bool = False) -> dict[str, Any]:
    """按回合数 + 天数截断账本，并清掉不再被引用的 blob。

    没保留期的复制就是磁盘炸弹 —— 这台机器上已经有一份 5 GB 的前例了。
    """
    rows = _read_lines(root)
    cutoff = time.time() - max(1, int(max_age_days)) * 86400
    kept = [row for row in rows if float(row.get("ts") or 0) >= cutoff]
    turns = []
    for row in kept:
        key = str(row.get("turn") or "")
        if key and key not in turns:
            turns.append(key)
    if len(turns) > int(keep_turns):
        allowed = set(turns[-int(keep_turns):])
        kept = [row for row in kept if not row.get("turn") or str(row["turn"]) in allowed]
    dropped = len(rows) - len(kept)
    referenced = {str(row.get("before_sha") or "") for row in kept}
    referenced |= {str(row.get("after_sha") or "") for row in kept if row.get("after_restorable")}
    referenced.discard("")
    removed_blobs = 0
    if apply and dropped:
        target = _ledger(root)
        tmp = target + ".tmp"
        with _LOCK:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                for row in kept:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            os.replace(tmp, target)
        base = os.path.join(_dir(root), "blobs")
        for shard in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            shard_dir = os.path.join(base, shard)
            if not os.path.isdir(shard_dir):
                continue
            for name in sorted(os.listdir(shard_dir)):
                if name in referenced:
                    continue
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(shard_dir, name))
                    removed_blobs += 1
    return {"apply": bool(apply), "dropped_entries": dropped,
            "kept_entries": len(kept), "removed_blobs": removed_blobs}
