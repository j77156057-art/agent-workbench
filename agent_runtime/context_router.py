"""Budgeted context-source routing for Agent applications.

The router is deterministic and does not execute retrieval itself.  It selects
which evidence channels the Agent must use, narrows native tool exposure, and
builds bounded system messages.  Retrieval remains an audited tool call, so it
appears in traces and follows the same permission and failure policies.
"""
from __future__ import annotations

import re
import atexit
import hashlib
import json
import os
import sqlite3
import threading
import time
import tempfile
from contextlib import closing
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping


_CODE = re.compile(
    r"代码|源码|函数|方法|类|接口|实现|报错|异常|调用|文件|路径|配置|重构|修复|"
    r"godot|unity|unreal|blueprint|git|api|python|typescript|javascript|vue|c#|cpp|"
    r"\b(?:class|def|function|method|stack|trace|bug)\b",
    re.I,
)
_PROJECT_AUDIT = re.compile(
    r"(?:当前|目前|现有|这个).*(?:游戏|项目).*(?:bug|问题|异常|故障)|"
    r"(?:游戏|项目).*(?:有什么|有哪些|哪些).*(?:bug|问题|异常|故障)|"
    r"(?:试玩|运行|跑起来).*(?:问题|异常|bug|故障)",
    re.I,
)
_KNOWLEDGE = re.compile(r"知识库|文档|资料|规范|手册|说明书|上传|教程|定义|讲了什么", re.I)
_WEB = re.compile(
    r"联网|网上|最新|近期|今天|天气|气温|当前版本|搜索|github|b站|哔哩哔哩|官方文档|下载地址|"
    r"\b(?:latest|current|news|release|github|bilibili)\b",
    re.I,
)
_MCP_DISCOVERY = re.compile(
    r"(?:mcp|连接器).*(?:搜|找|连接|接入|添加|安装|配置|使用|调用)|"
    r"(?:搜|找|连接|接入|添加|安装|配置|使用|调用).*(?:mcp|连接器)|"
    r"(?:eda|kicad|altium|easyeda|pcb).*(?:mcp|连接器)", re.I,
)
_ADAPTER_DISCOVERY = re.compile(
    r"适配器|预览适配器|领域适配|连接能力|支持(?:某|这个|该)?(?:软件|工具|领域|格式)|"
    r"(?:为|给).{0,24}(?:软件|工具|领域|格式).{0,24}(?:预览|连接|适配)", re.I,
)
_EXPERIENCE = re.compile(r"再次|之前|历史|经验|类似问题|复现|回归|修复|失败", re.I)
_TERM_SPLIT = re.compile(r"[\s,，、/|;；:：()（）\[\]【】]+")

CORE_GROUPS = frozenset({"general", "orchestration"})
CONTEXT_LAYERS = ("route", "project", "task", "subagent", "output")
_LAYER_WEIGHTS = {"route": 0.14, "project": 0.10, "task": 0.24,
                  "subagent": 0.34, "output": 0.18}
_COMPRESSION_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="docmind-context")

#: `os.replace` 的重试次数与退避基数（秒）。见 `_write` 里的说明。
_REPLACE_RETRIES = 6
_REPLACE_BACKOFF = 0.05
# SQLite connections are intentionally short-lived, but the foreground save
# and the durable worker can still touch the same database at the same time.
# Serialize the complete connection/transaction lifecycle.  This avoids
# native SQLite races on the bundled Python 3.13 build while retaining
# cross-process locking through SQLite's own busy timeout.
_COMPRESSION_SQL_LOCK = threading.RLock()
atexit.register(_COMPRESSION_EXECUTOR.shutdown, wait=False, cancel_futures=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _compression_digest(layers: Mapping[str, Any], max_chars: int) -> str:
    payload = json.dumps({"layers": dict(layers or {}), "max_chars": int(max_chars)},
                         ensure_ascii=False, sort_keys=True, default=str,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:32]


def _new_job_id() -> str:
    import uuid
    return "ctx-" + uuid.uuid4().hex


class PersistentCompressionWorker:
    """Durable SQLite queue for five-layer compression jobs."""

    schema = 1

    def __init__(self, db_path: str, *, start_thread: bool = True):
        self.db_path = str(db_path)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._init_db()
        if start_thread:
            self._thread = threading.Thread(target=self._run,
                                            name="docmind-context-worker", daemon=True)
            self._thread.start()

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Do not toggle journal_mode on every connection.  The compression
        # worker and a foreground save can open the same database at once;
        # changing WAL mode concurrently is unsafe on the bundled Python 3.13
        # SQLite build and has caused native access violations in _connect.
        # SQLite's default journal is sufficient here because all writes use
        # a bounded busy timeout and the queue is tiny.
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with _COMPRESSION_SQL_LOCK, closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS context_compression_jobs (
                    job_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    max_chars INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    result TEXT,
                    result_digest TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS context_compression_state_idx
                    ON context_compression_jobs(state, updated_at);
                """
            )
            conn.commit()

    def submit(self, layers: Mapping[str, Any] | None, max_chars: int,
               *, job_id: str | None = None) -> str:
        payload = {str(key): value for key, value in (layers or {}).items()}
        digest = _compression_digest(payload, max_chars)
        job_id = str(job_id or _new_job_id())
        now = _utc_now()
        with _COMPRESSION_SQL_LOCK, closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO context_compression_jobs
                   (job_id, schema_version, state, source_digest, max_chars, payload,
                    created_at, updated_at)
                   VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET
                    source_digest=excluded.source_digest, max_chars=excluded.max_chars,
                    payload=excluded.payload, state='queued', result=NULL,
                    result_digest=NULL, error=NULL, updated_at=excluded.updated_at""",
                (job_id, self.schema, digest, int(max_chars),
                 json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")),
                 now, now),
            )
            conn.commit()
        self._wake.set()
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        with _COMPRESSION_SQL_LOCK, closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM context_compression_jobs WHERE job_id = ?",
                               (str(job_id),)).fetchone()
        if row is None:
            return None
        item = dict(row)
        for key in ("payload", "result"):
            if item.get(key):
                try:
                    item[key] = json.loads(item[key])
                except (TypeError, ValueError):
                    item[key] = None
        return item

    def wait(self, job_id: str, timeout: float = 2.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            row = self.get(job_id)
            if row is None or row.get("state") in {"completed", "failed"}:
                return row
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return row
            self._wake.wait(min(0.025, remaining))
            self._wake.clear()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)
        # Tests and embedding callers sometimes close the returned worker
        # directly instead of using close_persistent_compression_worker().
        # Remove the exact instance from the process cache so a later window
        # cannot receive a stopped worker for the same database path.
        lock = globals().get("_PERSISTENT_WORKERS_LOCK")
        workers = globals().get("_PERSISTENT_WORKERS")
        if lock is not None and workers is not None:
            with lock:
                if workers.get(self.db_path) is self:
                    workers.pop(self.db_path, None)

    def _claim(self) -> dict[str, Any] | None:
        with _COMPRESSION_SQL_LOCK, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM context_compression_jobs
                   WHERE state='queued' OR (state='running' AND julianday(updated_at) < julianday('now','-60 seconds'))
                   ORDER BY created_at LIMIT 1"""
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE context_compression_jobs SET state='running', attempts=attempts+1, updated_at=? WHERE job_id=?",
                (_utc_now(), row["job_id"]),
            )
            conn.commit()
            item = dict(row)
            item["state"] = "running"
            return item

    def _finish(self, job_id: str, *, result: dict[str, Any] | None = None,
                error: str = "") -> None:
        state = "completed" if result is not None and not error else "failed"
        encoded = json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")) if result is not None else None
        digest = hashlib.sha256(encoded.encode("utf-8", "replace")).hexdigest()[:32] if encoded else ""
        with _COMPRESSION_SQL_LOCK, closing(self._connect()) as conn:
            conn.execute(
                "UPDATE context_compression_jobs SET state=?, result=?, result_digest=?, error=?, updated_at=? WHERE job_id=?",
                (state, encoded, digest, str(error or "")[:300], _utc_now(), str(job_id)),
            )
            conn.commit()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            job = None
            try:
                job = self._claim()
                if job:
                    payload = json.loads(str(job.get("payload") or "{}"))
                    snapshots, meta = compress_layers(payload, int(job.get("max_chars") or 800))
                    meta = dict(meta, source_digest=str(job.get("source_digest") or ""),
                                result_digest=layer_snapshot_digest(snapshots),
                                job_id=str(job.get("job_id")), schema_version=self.schema)
                    self._finish(str(job["job_id"]), result={"snapshots": snapshots, "meta": meta})
                    continue
            except Exception as exc:  # noqa: BLE001
                if job:
                    self._finish(str(job.get("job_id")), error=type(exc).__name__ + ": " + str(exc))
            self._wake.wait(0.2)
            self._wake.clear()


class JsonCompressionWorker:
    """Small atomic JSON queue used by default on the bundled Python runtime.

    It implements the same bounded queue surface as the SQLite worker without
    entering the native sqlite3 extension.  Writes use temp-file + replace so
    a window restart cannot expose a partially-written state file.  SQLite
    remains available as an explicit backend for deployments that need a
    multi-process queue.
    """

    schema = 1

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self.path = self.db_path[:-8] + ".json" if self.db_path.endswith(".sqlite3") else self.db_path + ".json"
        self._lock = threading.RLock()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.isfile(self.path):
            self._write({"schema": self.schema, "jobs": {}})

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict) and isinstance(raw.get("jobs"), dict):
                return raw
        except (OSError, ValueError, TypeError):
            pass
        return {"schema": self.schema, "jobs": {}}

    def _write(self, payload: Mapping[str, Any]) -> None:
        parent = os.path.dirname(self.path) or "."
        fd, temporary = tempfile.mkstemp(prefix=".context-compression-", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            # Windows 上 `os.replace` 会在目标正被另一个句柄读着时抛
            # `PermissionError: [WinError 5]`（压缩执行器有 2 个 worker，
            # 再加上杀毒/索引器也会短暂持有句柄）。这不是逻辑错误，重试即可。
            #
            # 实测（`wf-3784257fd6d1`）：未加重试时这一抛会让 `_save` 整个失败，
            # 于是 **`/choose` 直接 500**——一个派生账本写不进去，把主状态也赔上了。
            last: OSError | None = None
            for attempt in range(_REPLACE_RETRIES):
                try:
                    os.replace(temporary, self.path)
                    last = None
                    break
                except PermissionError as exc:
                    last = exc
                    if attempt + 1 >= _REPLACE_RETRIES:
                        break
                    time.sleep(_REPLACE_BACKOFF * (attempt + 1))
            if last is not None:
                raise last
        finally:
            try:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            except OSError:
                pass

    def submit(self, layers: Mapping[str, Any] | None, max_chars: int,
               *, job_id: str | None = None) -> str:
        source = dict(layers or {})
        job_id = str(job_id or _new_job_id())
        now = _utc_now()
        row = {
            "job_id": job_id, "schema_version": self.schema, "state": "queued",
            "source_digest": _compression_digest(source, max_chars),
            "max_chars": int(max_chars), "payload": source, "result": None,
            "result_digest": "", "error": "", "attempts": 0,
            "created_at": now, "updated_at": now,
        }
        with self._lock:
            data = self._read()
            data.setdefault("jobs", {})[job_id] = row
            self._write(data)
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._read().get("jobs", {}).get(str(job_id))
            return dict(row) if isinstance(row, Mapping) else None

    def wait(self, job_id: str, timeout: float = 2.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            row = self.get(job_id)
            if row is None or row.get("state") in {"completed", "failed"}:
                return row
            if time.monotonic() >= deadline:
                return row
            time.sleep(0.025)

    def _finish(self, job_id: str, *, result: dict[str, Any] | None = None,
                error: str = "") -> None:
        state = "completed" if result is not None and not error else "failed"
        encoded = json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")) if result is not None else ""
        with self._lock:
            data = self._read()
            row = dict(data.setdefault("jobs", {}).get(str(job_id)) or {})
            row.update({
                "state": state, "result": result, "result_digest": hashlib.sha256(
                    encoded.encode("utf-8", "replace")).hexdigest()[:32] if encoded else "",
                "error": str(error or "")[:300], "attempts": int(row.get("attempts") or 0) + 1,
                "updated_at": _utc_now(),
            })
            data["jobs"][str(job_id)] = row
            self._write(data)

    def close(self) -> None:
        workers = globals().get("_PERSISTENT_WORKERS")
        lock = globals().get("_PERSISTENT_WORKERS_LOCK")
        if workers is not None and lock is not None:
            with lock:
                if workers.get(self.db_path) is self:
                    workers.pop(self.db_path, None)


_PERSISTENT_WORKERS: dict[str, Any] = {}
_PERSISTENT_WORKERS_LOCK = threading.RLock()


def persistent_compression_worker(db_path: str | None = None, *, start_thread: bool = True) -> Any:
    path = str(db_path or os.path.join(os.getenv("DOCMIND_STATE_ROOT", "."),
                                       ".docmind", "context_compression.sqlite3"))
    backend = os.getenv("DOCMIND_CONTEXT_COMPRESSION_BACKEND", "json").strip().lower()
    if backend not in {"sqlite", "json"}:
        backend = "json"
    if backend == "json":
        if not start_thread:
            return JsonCompressionWorker(path)
        with _PERSISTENT_WORKERS_LOCK:
            worker = _PERSISTENT_WORKERS.get(path)
            if worker is None or not isinstance(worker, JsonCompressionWorker):
                worker = JsonCompressionWorker(path)
                _PERSISTENT_WORKERS[path] = worker
            return worker
    if not start_thread:
        # Synchronous callers should not enter the process-wide worker cache;
        # they own this short-lived store and close it after the transaction.
        return PersistentCompressionWorker(path, start_thread=False)
    with _PERSISTENT_WORKERS_LOCK:
        worker = _PERSISTENT_WORKERS.get(path)
        if worker is None:
            worker = PersistentCompressionWorker(path)
            _PERSISTENT_WORKERS[path] = worker
        return worker


def close_persistent_compression_worker(db_path: str) -> None:
    path = str(db_path)
    with _PERSISTENT_WORKERS_LOCK:
        worker = _PERSISTENT_WORKERS.pop(path, None)
    if worker is not None:
        worker.close()


@dataclass(frozen=True)
class ContextPlan:
    sources: tuple[str, ...]
    tool_groups: frozenset[str]
    skill_names: tuple[str, ...]
    messages: tuple[str, ...]
    budget_chars: int
    used_chars: int

    def allows_group(self, group: str) -> bool:
        return group in self.tool_groups


@dataclass(frozen=True)
class CompressionResult:
    text: str
    original_chars: int
    compressed_chars: int
    truncated: bool


def allocate_layer_budgets(total_chars: int, *, weights: Mapping[str, float] | None = None,
                           minimum_chars: int = 80) -> dict[str, int]:
    """Allocate a deterministic character budget across the five context layers."""
    total = max(len(CONTEXT_LAYERS) * max(1, int(minimum_chars)), int(total_chars))
    selected = {layer: float((weights or _LAYER_WEIGHTS).get(layer, 0.0))
                for layer in CONTEXT_LAYERS}
    weight_sum = sum(max(0.0, value) for value in selected.values()) or 1.0
    minimum = max(1, int(minimum_chars))
    remaining = total - minimum * len(CONTEXT_LAYERS)
    raw = {layer: minimum + remaining * max(0.0, weight) / weight_sum
           for layer, weight in selected.items()}
    out = {layer: int(value) for layer, value in raw.items()}
    # Preserve the exact total after flooring, assigning remainder by the
    # largest fractional parts so repeated recovery is stable.
    remainder = total - sum(out.values())
    for layer, _ in sorted(raw.items(), key=lambda pair: pair[1] - int(pair[1]), reverse=True):
        if remainder <= 0:
            break
        out[layer] += 1
        remainder -= 1
    return out


def compress_text(value: Any, max_chars: int, *, head_ratio: float = 0.65) -> CompressionResult:
    """Deterministically bound task context while retaining head and tail evidence."""
    text = str(value or "").replace("\x00", "").strip()
    limit = max(40, int(max_chars))
    if len(text) <= limit:
        return CompressionResult(text, len(text), len(text), False)
    marker = "\n…【上下文已压缩】…\n"
    available = max(1, limit - len(marker))
    head = max(1, min(available - 1, int(available * max(0.2, min(0.8, head_ratio)))))
    tail = max(1, available - head)
    compact = text[:head].rstrip() + marker + text[-tail:].lstrip()
    return CompressionResult(compact, len(text), len(compact), True)


def compress_context(values: Mapping[str, Any] | None, max_chars: int) -> tuple[dict[str, str], dict[str, Any]]:
    """Allocate a bounded budget across dependency conclusions.

    Each dependency receives a fair share, while a final pass enforces the
    aggregate budget.  The returned metadata is safe to put in a trace.
    """
    source = {str(key): str(value or "") for key, value in (values or {}).items()}
    if not source:
        return {}, {"original_chars": 0, "compressed_chars": 0, "truncated": False, "items": 0}
    budget = max(80, int(max_chars))
    share = max(40, budget // max(1, len(source)))
    out: dict[str, str] = {}
    original = 0
    truncated = False
    for key in sorted(source):
        result = compress_text(source[key], share)
        out[key] = result.text
        original += result.original_chars
        truncated = truncated or result.truncated
    # Fair shares can exceed the aggregate budget due to the minimum share.
    joined = sum(len(value) for value in out.values())
    if joined > budget:
        keys = list(out)
        remaining = budget
        for index, key in enumerate(keys):
            allowance = max(0, remaining // max(1, len(keys) - index))
            if allowance < 40:
                out[key] = out[key][:allowance]
                truncated = True
            else:
                result = compress_text(out[key], allowance)
                out[key] = result.text
                truncated = truncated or result.truncated
            remaining = max(0, remaining - len(out[key]))
    return out, {"original_chars": original, "compressed_chars": sum(len(v) for v in out.values()),
                 "truncated": truncated, "items": len(out)}


def compress_context_async(values: Mapping[str, Any] | None, max_chars: int) -> Future:
    """Start bounded compression before a Subagent wave is dispatched.

    The default is a completed Future backed by the caller thread.  Set
    ``DOCMIND_CONTEXT_COMPRESSION_ASYNC=1`` to use the shared executor; the
    synchronous default avoids Python 3.13 native-thread instability while
    keeping the same Future-based API for callers.
    """
    snapshot = dict(values or {})
    if os.getenv("DOCMIND_CONTEXT_COMPRESSION_ASYNC", "0").strip().lower() not in {
            "1", "true", "yes", "on"}:
        future: Future = Future()
        try:
            future.set_result(compress_context(snapshot, int(max_chars)))
        except Exception as exc:  # pragma: no cover - mirrors executor behavior
            future.set_exception(exc)
        return future
    return _COMPRESSION_EXECUTOR.submit(compress_context, snapshot, int(max_chars))


def summarize_subagent_result(result: Mapping[str, Any] | None,
                              max_chars: int = 1200) -> tuple[str, dict[str, Any]]:
    """Build a bounded dependency summary for the next Subagent.

    Only the conclusion and execution metadata are carried forward.  Tool
    arguments, full observations and arbitrary result fields remain local to
    the completed task and do not consume the next task's context budget.
    """
    result = dict(result or {})
    raw_steps = (result.get("trace") or {}).get("steps")
    steps = list(raw_steps) if isinstance(raw_steps, (list, tuple)) else []
    failures = [step for step in steps if isinstance(step, Mapping) and step.get("ok") is False]
    conclusion = str(result.get("conclusion") or result.get("text") or result.get("merged") or "").strip()
    lines = ["状态：%s" % str(result.get("status") or "unknown")[:40]]
    if conclusion:
        lines.append("结论：%s" % conclusion)
    lines.append("步骤：%s；失败工具步骤：%s" % (int(result.get("steps") or len(steps)), len(failures)))
    if result.get("error"):
        lines.append("错误类别：%s" % type(result.get("error")).__name__)
    compact = compress_text("\n".join(lines), max_chars)
    return compact.text, {
        "original_chars": sum(len(line) for line in lines),
        "compressed_chars": compact.compressed_chars,
        "truncated": compact.truncated,
        "steps": len(steps),
        "failed_steps": len(failures),
    }


def compress_layers(layers: Mapping[str, Any] | None, max_chars: int) -> tuple[dict[str, str], dict[str, Any]]:
    """Create a durable five-layer snapshot for cross-window recovery.

    The snapshot contains only bounded text, not raw tool arguments or full
    observations. Its digest lets a resumed process verify the restored layer
    payload without keeping a second unbounded history.
    """
    budgets = allocate_layer_budgets(max_chars)
    snapshots: dict[str, str] = {}
    original = 0
    compressed = 0
    truncated = False
    for layer in CONTEXT_LAYERS:
        value = (layers or {}).get(layer, "")
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        result = compress_text(text, budgets[layer])
        snapshots[layer] = result.text
        original += result.original_chars
        compressed += result.compressed_chars
        truncated = truncated or result.truncated
    digest = hashlib.sha256(
        json.dumps(snapshots, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return snapshots, {
        "schema": "five-layer-snapshot-v1",
        "budgets": budgets,
        "original_chars": original,
        "compressed_chars": compressed,
        "truncated": truncated,
        "digest": digest,
    }


def layer_snapshot_digest(snapshots: Mapping[str, Any] | None) -> str:
    """Return the stable digest used to verify a recovered snapshot."""
    return hashlib.sha256(
        json.dumps(dict(snapshots or {}), ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]


def compress_layers_durable(layers: Mapping[str, Any] | None, max_chars: int,
                            *, db_path: str | None = None, timeout: float = 2.0
                            ) -> tuple[dict[str, str], dict[str, Any]]:
    """Compress through the durable queue and verify the returned snapshot.

    By default the queue record is completed synchronously, which avoids a
    native-thread SQLite dependency on Python 3.13. Set
    ``DOCMIND_CONTEXT_COMPRESSION_ASYNC=1`` to use the background worker; if
    that worker is restarting or the deadline expires, the queued job stays in
    SQLite and a synchronous result is returned for the current save.
    """
    source = dict(layers or {})
    backend = os.getenv("DOCMIND_CONTEXT_COMPRESSION_BACKEND", "json").strip().lower()
    async_enabled = backend == "sqlite" and os.getenv(
        "DOCMIND_CONTEXT_COMPRESSION_ASYNC", "0").strip().lower() in {
            "1", "true", "yes", "on"
        }
    if not async_enabled:
        # The durable record is still written, but compression is completed in
        # the caller.  This is the stable default on the bundled Python 3.13
        # runtime, where a background SQLite worker can crash in native code
        # even when ordinary transaction locking is correct.
        worker = persistent_compression_worker(db_path, start_thread=False)
        try:
            job_id = worker.submit(source, max_chars)
            snapshots, meta = compress_layers(source, max_chars)
            durable_meta = dict(meta,
                                source_digest=_compression_digest(source, max_chars),
                                result_digest=layer_snapshot_digest(snapshots),
                                job_id=job_id, schema_version=worker.schema)
            worker._finish(job_id, result={"snapshots": snapshots,
                                           "meta": durable_meta})
            durable_meta["job_state"] = "completed"
            return snapshots, durable_meta
        finally:
            worker.close()

    worker = persistent_compression_worker(db_path)
    job_id = worker.submit(source, max_chars)
    row = worker.wait(job_id, timeout=timeout)
    if row and row.get("state") == "completed" and isinstance(row.get("result"), Mapping):
        result = row["result"]
        snapshots = dict(result.get("snapshots") or {})
        meta = dict(result.get("meta") or {})
        if layer_snapshot_digest(snapshots) == str(meta.get("result_digest") or ""):
            meta["job_state"] = "completed"
            return snapshots, meta
    snapshots, meta = compress_layers(source, max_chars)
    meta = dict(meta, job_id=job_id,
                job_state=str((row or {}).get("state") or "fallback"),
                source_digest=_compression_digest(source, max_chars),
                result_digest=layer_snapshot_digest(snapshots))
    return snapshots, meta


def _close_persistent_workers() -> None:
    with _PERSISTENT_WORKERS_LOCK:
        workers = list(_PERSISTENT_WORKERS.values())
        _PERSISTENT_WORKERS.clear()
    for worker in workers:
        worker.close()


atexit.register(_close_persistent_workers)


def _actual_question(text: str) -> str:
    marker = "用户问题："
    index = (text or "").rfind(marker)
    return text[index + len(marker):].strip() if index >= 0 else (text or "").strip()


def _skill_score(question: str, item: dict[str, Any]) -> int:
    haystack = question.lower()
    score = 0
    name = str(item.get("name") or "").strip().lower()
    if name and name in haystack:
        score += 8
    metadata = " ".join((str(item.get("description") or ""),
                         str(item.get("when_to_use") or ""))).lower()
    for term in _TERM_SPLIT.split(metadata):
        if len(term) >= 2 and term in haystack:
            score += min(len(term), 6)
    return score


class ContextRouter:
    def __init__(self, max_chars: int = 6000, max_skills: int = 6):
        self.max_chars = max(800, int(max_chars))
        self.max_skills = max(1, int(max_skills))

    def route(
        self,
        question: str,
        *,
        code_root: str = "",
        ingested_sources: Iterable[str] = (),
        web_enabled: bool = False,
        experience_enabled: bool = False,
        skill_items: Iterable[dict[str, Any]] = (),
        experience_recall: Callable[[str], list[dict[str, Any]]] | None = None,
        token_budget: int = 0,
    ) -> ContextPlan:
        query = _actual_question(question)
        sources: list[str] = []
        groups = set(CORE_GROUPS)

        wants_code = bool(code_root and _CODE.search(query))
        has_knowledge = bool(tuple(ingested_sources)) or "知识库中已上传" in (question or "")
        wants_knowledge = bool(has_knowledge and _KNOWLEDGE.search(query))
        wants_web = bool(web_enabled and _WEB.search(query))
        wants_mcp = bool(_MCP_DISCOVERY.search(query))
        wants_adapter = bool(_ADAPTER_DISCOVERY.search(query))
        wants_experience = bool(experience_enabled and _EXPERIENCE.search(query))

        # Ambiguous questions may use both local stores.  A configured code
        # project alone does not force code RAG for ordinary conversation.
        if wants_code:
            sources.append("code")
            groups.update(("code", "developer", "game"))
        if wants_knowledge:
            sources.append("knowledge")
            groups.add("knowledge")
        if wants_web:
            sources.append("web")
            groups.add("web")
        if wants_mcp:
            groups.add("developer")
        if wants_adapter:
            groups.add("developer")
        if wants_experience:
            sources.append("experience")
        if not sources:
            sources.append("direct")
        # 联网开关只表示“允许”，不等于本轮一定需要联网。只有命中
        # 时效/外部资料意图时才把 web 工具组加入本轮暴露集合，避免
        # progressive 模式下每个普通问题都携带整组外网工具。

        ranked = sorted(
            ((_skill_score(query, item), item) for item in skill_items),
            key=lambda pair: (-pair[0], str(pair[1].get("name") or "")),
        )
        selected = [item for score, item in ranked if score > 0][:self.max_skills]
        if selected:
            sources.append("skill")
            groups.add("developer")

        # Context gets a bounded fraction of the prompt.  Four chars/token is
        # conservative for mixed Chinese and code and prevents fixed metadata
        # from consuming the history/tool budget.
        dynamic_cap = int(token_budget * 4 * 0.16) if token_budget else self.max_chars
        budget = max(800, min(self.max_chars, dynamic_cap))
        messages: list[str] = []
        used = 0

        def add(text: str) -> None:
            nonlocal used
            text = (text or "").strip()
            remaining = budget - used
            if not text or remaining <= 0:
                return
            if len(text) > remaining:
                text = text[:max(0, remaining - 1)].rstrip() + "…"
            messages.append(text)
            used += len(text)

        route_lines = ["【上下文路由】本轮证据来源：" + "、".join(sources) + "。"]
        if wants_code:
            route_lines.append("代码问题先调用 search_code 定位，再用 read_file/grep 核对原文；不得用知识库片段代替代码证据。")
        if _PROJECT_AUDIT.search(query):
            route_lines.append(
                "【当前项目缺陷审查】用户要检查当前游戏/项目，不是查询 bugs/ 历史归档。"
                "先用 search_code/grep/read_file 获取当前项目证据；项目正在运行且有引擎连接器时，"
                "再用 dev_list_connectors/dev_route_connector/dev_mcp_call 或 game_playtest 获取运行证据。"
                "dev_list_bugs 只能作为最后的历史记录补充，不能作为当前缺陷结论或唯一证据。"
            )
        if wants_knowledge:
            route_lines.append("资料问题先调用 search_knowledge，回答时保留命中片段中的来源名称。")
        if wants_web:
            route_lines.append("需要时效信息时使用 web_search/web_research，并在回答中附来源 URL。")
        elif _WEB.search(query) and not web_enabled:
            route_lines.append("问题可能需要最新外部信息，但联网未开启；应明确说明限制，不得编造实时结果。")
        if wants_mcp:
            route_lines.append(
                "需要新 MCP 连接器时先调用 dev_mcp_search 查真实候选并展示给用户；"
                "联网开关关闭时该工具只给离线指引。搜索结果不代表已连接，"
                "添加连接器和启用能力仍需用户确认。")
        if wants_adapter:
            route_lines.append(
                "用户在询问新的领域或预览适配能力：先查现有 MCP/连接器和工具；"
                "只有现有能力不足时才调用 dev_preview_adapter_create 生成当前项目的适配器草稿。"
                "如果需要转换逻辑，可同时生成受控 Python/Node 的 adapt(payload) 模块；"
                "先向用户展示领域、产物、刷新方式、代码摘要和验收条件，用户明确确认后才调用 "
                "dev_preview_adapter_approve( decision: approve ) 激活；不得执行未审批或未通过静态检查的代码。"
            )
        add("\n".join(route_lines))

        if selected:
            lines = ["【相关技能目录】只提供元数据；使用前调用 dev_use_skill 读取正文："]
            for item in selected:
                line = "- %s：%s" % (item.get("name"), item.get("description") or "专项流程")
                if item.get("when_to_use"):
                    line += "（适用：%s）" % item["when_to_use"]
                lines.append(line)
            add("\n".join(lines))

        if wants_experience and experience_recall is not None:
            try:
                hits = experience_recall(query) or []
            except Exception:
                hits = []
            if hits:
                lines = ["【历史经验（建议性，不覆盖当前证据）】"]
                for hit in hits[:5]:
                    meta = hit.get("metadata") or {}
                    stale = "（陈旧）" if hit.get("stale") else ""
                    lines.append("- [%s]%s %s | 决策：%s | 教训：%s" % (
                        meta.get("outcome"), stale, meta.get("action_summary", ""),
                        meta.get("decision", ""), meta.get("lesson") or "（无）"))
                add("\n".join(lines))

        return ContextPlan(
            sources=tuple(dict.fromkeys(sources)),
            tool_groups=frozenset(groups),
            skill_names=tuple(str(item.get("name")) for item in selected),
            messages=tuple(messages),
            budget_chars=budget,
            used_chars=used,
        )
