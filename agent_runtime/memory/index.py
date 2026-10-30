"""共享记忆的派生索引（SQLite）。文件是事实来源，本索引删掉可重建。

P0 只索引 `shared/facts/*.md` 与 `shared/lessons/*.md` 的 frontmatter 条目；
向量列留好但暂不写入（P3/P4 接 embeddings）。重建按 body_hash 判断变更，
手工改文件也能被发现。
"""
from __future__ import annotations

import glob
import os
import sqlite3

from . import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'fact',
  scope TEXT NOT NULL DEFAULT 'global',
  status TEXT NOT NULL DEFAULT 'active',
  path TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  body_hash TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.5,
  importance REAL NOT NULL DEFAULT 0.5,
  usage INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL DEFAULT '',
  updated TEXT NOT NULL DEFAULT '',
  last_used TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_items_path ON items(path);
CREATE INDEX IF NOT EXISTS idx_items_scope ON items(scope, status);
CREATE TABLE IF NOT EXISTS edges(
  src TEXT NOT NULL, dst TEXT NOT NULL, type TEXT NOT NULL,
  PRIMARY KEY(src, dst, type)
);
CREATE TABLE IF NOT EXISTS vectors(
  id TEXT PRIMARY KEY, model TEXT NOT NULL DEFAULT '',
  dim INTEGER NOT NULL DEFAULT 0, vec BLOB, body_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
"""

_PATTERNS = ("shared/facts/*.md", "shared/lessons/*.md",
             "projects/*/facts/*.md", "projects/*/lessons/*.md")


def path():
    return store.safe_path("shared", "index.sqlite3")


def _connect():
    target = path()
    store.ensure_dir(os.path.dirname(target))
    conn = sqlite3.connect(target, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS items_fts "
                     "USING fts5(id UNINDEXED, title, body)")
    except sqlite3.OperationalError:
        pass
    return conn


def _fts(conn):
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE name='items_fts'").fetchone() is not None
    except sqlite3.Error:
        return False


def scan_files():
    base = os.path.abspath(store.root())
    found = []
    for pattern in _PATTERNS:
        for item in sorted(glob.glob(os.path.join(base, *pattern.split("/")))):
            if os.path.isfile(item):
                found.append((item, os.path.relpath(item, base).replace("\\", "/")))
    return found


def _clamp(value, default=0.5):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _item_from_file(absolute, relative):
    header, body = store.split_frontmatter(store.read_text(absolute))
    if header is None:
        raise ValueError("缺 frontmatter")
    body = str(body or "").strip()
    title = str(header.get("title") or "").strip()
    if not title:
        title = (body.splitlines()[0][:80] if body else "")
    edges = []
    for raw in header.get("edges") or []:
        if not isinstance(raw, dict):
            continue
        target = str(raw.get("to") or raw.get("dst") or "").strip()
        kind = str(raw.get("type") or "").strip()
        if target and kind:
            edges.append({"type": kind, "to": target})
    return {
        "id": str(header.get("id") or "").strip() or "mem-" + store.sha256_text(relative)[:16],
        "kind": str(header.get("kind") or "fact").strip(),
        "scope": str(header.get("scope") or "global").strip(),
        "status": str(header.get("status") or "active").strip(),
        "path": relative,
        "title": title or str(header.get("id") or relative),
        "body": body,
        "body_hash": store.sha256_text(body),
        "confidence": _clamp(header.get("confidence")),
        "importance": _clamp(header.get("importance")),
        "usage": int(header.get("usage") or 0),
        "created": str(header.get("created") or ""),
        "updated": str(header.get("updated") or ""),
        "last_used": str(header.get("last_used") or ""),
        "edges": edges,
    }


def rebuild(verify=False):
    """扫文件重建索引。幂等：文件没变则不动，文件消失则删行。"""
    stats = {"files": 0, "inserted": 0, "updated": 0, "unchanged": 0,
             "removed": 0, "edges": 0, "skipped": [], "fts": False, "verified": None}
    conn = _connect()
    try:
        fts = _fts(conn)
        stats["fts"] = fts
        existing = {row["path"]: row["body_hash"]
                    for row in conn.execute("SELECT path, body_hash FROM items")}
        seen = set()
        with conn:
            for absolute, relative in scan_files():
                stats["files"] += 1
                seen.add(relative)
                try:
                    item = _item_from_file(absolute, relative)
                except Exception as exc:  # noqa: BLE001 —— 单个坏文件不拖垮整次重建
                    if len(stats["skipped"]) < 20:
                        stats["skipped"].append({"path": relative, "error": str(exc)[:160]})
                    continue
                if relative not in existing:
                    stats["inserted"] += 1
                elif existing[relative] != item["body_hash"]:
                    stats["updated"] += 1
                else:
                    stats["unchanged"] += 1
                    continue
                conn.execute("DELETE FROM items WHERE path=?", (relative,))
                conn.execute(
                    "INSERT INTO items(id,kind,scope,status,path,title,body_hash,"
                    "confidence,importance,usage,created,updated,last_used) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item["id"], item["kind"], item["scope"], item["status"], item["path"],
                     item["title"], item["body_hash"], item["confidence"], item["importance"],
                     item["usage"], item["created"], item["updated"], item["last_used"]))
                conn.execute("DELETE FROM edges WHERE src=?", (item["id"],))
                for edge in item["edges"]:
                    conn.execute("INSERT OR REPLACE INTO edges(src,dst,type) VALUES(?,?,?)",
                                 (item["id"], edge["to"], edge["type"]))
                    stats["edges"] += 1
                if fts:
                    conn.execute("DELETE FROM items_fts WHERE id=?", (item["id"],))
                    conn.execute("INSERT INTO items_fts(id,title,body) VALUES(?,?,?)",
                                 (item["id"], item["title"], item["body"]))
            for relative in sorted(set(existing) - seen):
                row = conn.execute("SELECT id FROM items WHERE path=?", (relative,)).fetchone()
                if not row:
                    continue
                conn.execute("DELETE FROM items WHERE path=?", (relative,))
                conn.execute("DELETE FROM edges WHERE src=? OR dst=?", (row["id"], row["id"]))
                if fts:
                    conn.execute("DELETE FROM items_fts WHERE id=?", (row["id"],))
                stats["removed"] += 1
            conn.execute("INSERT INTO meta(key,value) VALUES('rebuilt_at',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (store.now(),))
        count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        if verify:
            stats["verified"] = count == len(seen)
        stats["items"] = count
        return stats
    finally:
        conn.close()


def stats():
    conn = _connect()
    try:
        counts = {"items": conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                  "edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
                  "vectors": conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]}
        meta = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM meta")}
        counts["rebuilt_at"] = meta.get("rebuilt_at") or ""
        counts["fts"] = _fts(conn)
        return counts
    finally:
        conn.close()
