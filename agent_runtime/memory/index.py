"""层级记忆的派生索引（SQLite）。文件是事实来源，本索引删掉可重建。

P1 起索引共享条目与项目候选，支持关键词检索、1 跳关联扩展；
向量列留好但暂不写入，语义召回仍在 embeddings 侧按需接入。
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import sqlite3
from datetime import datetime

from . import graph, score, store

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'fact',
  scope TEXT NOT NULL DEFAULT 'global',
  project TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT 'main',
  status TEXT NOT NULL DEFAULT 'active',
  path TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  body_hash TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.5,
  importance REAL NOT NULL DEFAULT 0.5,
  pinned INTEGER NOT NULL DEFAULT 0,
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

# 最近一次 owner 迁移的备份路径（只在补列那次非空），供 migrate_owner 汇报。
_LAST_OWNER_BACKUP = ""


def path():
    return store.safe_path("shared", "index.sqlite3")


def _backup_file(conn, target):
    """迁移前备份数据库（只在补列那次做）。文件名带秒级时间戳，可重复跑。

    用 sqlite3 在线备份 API 而不是拷文件：服务进程可能正在写库，
    直接 copy 可能撞上写入中途得到不一致副本；backup() 内部持锁取一致快照。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = "%s.bak-%s" % (target, stamp)
    dest = sqlite3.connect(backup)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return backup


def _ensure_owner(conn, target):
    """幂等补 owner 列：老库先备份，再 ALTER + 回填 main。

    新库由 SCHEMA 直接带上 owner，不会触发备份；重复调用只会看到列已存在，
    返回空字符串，因此可以安全地反复执行。
    """
    global _LAST_OWNER_BACKUP
    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if "owner" in columns:
        return ""
    backup = ""
    if os.path.isfile(target):
        backup = _backup_file(conn, target)
        _LAST_OWNER_BACKUP = backup
    conn.execute("ALTER TABLE items ADD COLUMN owner TEXT NOT NULL DEFAULT 'main'")
    conn.execute("UPDATE items SET owner='main' WHERE owner='' OR owner IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_owner ON items(owner)")
    conn.commit()
    return backup


def _connect():
    target = path()
    store.ensure_dir(os.path.dirname(target))
    conn = sqlite3.connect(target, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if "body" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN body TEXT NOT NULL DEFAULT ''")
    if "project" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN project TEXT NOT NULL DEFAULT ''")
    if "pinned" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    if "owner" not in columns:
        _ensure_owner(conn, target)
    else:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_owner ON items(owner)")
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS items_fts "
                     "USING fts5(id UNINDEXED, title, body)")
    except sqlite3.OperationalError:
        pass
    return conn


def migrate_owner(default="main"):
    """幂等迁移 owner 维度：老库先备份、补列并回填，返回可复核的计数。

    第一次在有数据的老库上调用：`added_column=True` 且 `backed_up` 指向备份文件，
    既有行全部归到 default（main），`backfilled` = 既有行数。第二次调用：列已存在，
    `added_column=False`、`backed_up=''`、`backfilled=0`，items 总数与 owner 分布保持不变。
    """
    target = path()
    existed = os.path.isfile(target)
    had_owner = False
    if existed:
        try:
            probe = sqlite3.connect("file:%s?mode=ro" % target.replace("\\", "/"), uri=True)
            had_owner = "owner" in {row[1] for row in probe.execute("PRAGMA table_info(items)")}
            probe.close()
        except sqlite3.Error:
            had_owner = False
    conn = _connect()
    try:
        with conn:
            cursor = conn.execute("UPDATE items SET owner=? WHERE owner='' OR owner IS NULL",
                                  (str(default),))
            backfilled = cursor.rowcount
            conn.execute("CREATE INDEX IF NOT EXISTS idx_items_owner ON items(owner)")
        owners = {row["owner"]: row["n"] for row in conn.execute(
            "SELECT owner, COUNT(*) n FROM items GROUP BY owner ORDER BY n DESC")}
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()
    migrated = existed and not had_owner
    # 补列的 ALTER ... DEFAULT 'main' 已把 owner 写进全部既有行，随后的 UPDATE
    # 只修空值（通常 0 行）。拿 cursor.rowcount 当「回填数」会恒 0，把「迁移了
    # N 行」藏掉；补列那次按既有行数报，其余按修复行数报。
    reported = int(total) if migrated else int(backfilled or 0)
    return {"ok": True, "db": target, "existed": existed,
            "added_column": migrated,
            "backed_up": _LAST_OWNER_BACKUP if migrated else "",
            "backfilled": reported, "total": int(total),
            "owners": owners}


def read_edges():
    """只读导出 `edges` 表（含两端条目的 scope），供记忆图呈现真实邻接。

    - 索引文件不存在 / 表不存在 / 被占用：返回空列表，由调用方显示“真实边 0 条”；
    - 绝不建库、不重建索引，`mode=ro` 打开，保证 GET 接口只读。
    """
    target = path()
    if not os.path.isfile(target):
        return []
    uri = "file:" + target.replace("\\", "/") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT e.src, e.dst, e.type, s.scope AS src_scope, d.scope AS dst_scope, "
            "s.project AS src_project, d.project AS dst_project "
            "FROM edges e LEFT JOIN items s ON s.id = e.src "
            "LEFT JOIN items d ON d.id = e.dst").fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    return [{"src": str(row["src"]), "dst": str(row["dst"]),
            "type": str(row["type"] or ""),
            "src_scope": str(row["src_scope"] or ""),
            "dst_scope": str(row["dst_scope"] or ""),
            "src_project": str(row["src_project"] or ""),
            "dst_project": str(row["dst_project"] or "")} for row in rows]


def _fts(conn):
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE name='items_fts'").fetchone() is not None
    except sqlite3.Error:
        return False


_EMBED_CLIENT = None
_EMBED_FAILED = False


def _embed_enabled():
    try:
        from config import get_memory_semantic
        if get_memory_semantic():
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from agent_memory import _embed_enabled as enabled
        return bool(enabled())
    except Exception:  # noqa: BLE001
        return False


def _embed_one(text):
    global _EMBED_CLIENT, _EMBED_FAILED
    if _EMBED_FAILED:
        return []
    if _EMBED_CLIENT is None:
        try:
            from embeddings import EmbeddingClient
            _EMBED_CLIENT = EmbeddingClient()
        except Exception:  # noqa: BLE001
            _EMBED_FAILED = True
            return []
    try:
        vectors = _EMBED_CLIENT.embed([str(text or "")[:4000]])
        return list(vectors[0]) if vectors and vectors[0] else []
    except Exception:  # noqa: BLE001 —— 嵌入失败退化为关键词
        return []


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _ensure_vector(conn, item):
    if not _embed_enabled():
        return
    row = conn.execute("SELECT body_hash FROM vectors WHERE id=?", (item["id"],)).fetchone()
    if row and row["body_hash"] == item["body_hash"]:
        return
    vector = _embed_one((item.get("title") or "") + "\n" + (item.get("body") or ""))
    if not vector:
        return
    conn.execute(
        "INSERT OR REPLACE INTO vectors(id,model,dim,vec,body_hash) VALUES(?,?,?,?,?)",
        (item["id"], str(getattr(_EMBED_CLIENT, "model", "") or ""), len(vector),
         json.dumps(vector), item["body_hash"]))


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
    parts = relative.split("/")
    project = parts[1] if len(parts) > 2 and parts[0] == "projects" else ""
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
        "project": project,
        "owner": str(header.get("owner") or "main").strip() or "main",
        "status": str(header.get("status") or "active").strip(),
        "path": relative,
        "title": title or str(header.get("id") or relative),
        "body": body,
        "body_hash": store.sha256_text(body),
        "confidence": _clamp(header.get("confidence")),
        "importance": _clamp(header.get("importance")),
        "pinned": 1 if header.get("pinned") else 0,
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
        existing = {row["path"]: (row["body_hash"], row["body"], row["project"],
                                  row["owner"], row["status"], row["title"], row["confidence"],
                                  row["importance"], row["pinned"], row["usage"],
                                  row["last_used"], row["updated"])
                    for row in conn.execute(
                        "SELECT path, body_hash, body, project, owner, status, title, confidence,"
                        " importance, pinned, usage, last_used, updated FROM items")}
        seen = set()
        seen_ids = set()
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
                if item["id"] in seen_ids:
                    if len(stats["skipped"]) < 20:
                        stats["skipped"].append({"path": relative,
                                                 "error": "重复 id（共享副本优先）"})
                    continue
                seen_ids.add(item["id"])
                signature = (item["body_hash"], item["body"], item["project"],
                             item["owner"], item["status"], item["title"], item["confidence"],
                             item["importance"], item["pinned"], item["usage"],
                             item["last_used"], item["updated"])
                if relative not in existing:
                    stats["inserted"] += 1
                    row_changed = True
                elif (existing[relative] != signature
                      or not existing[relative][1]
                      or (relative.startswith("projects/") and not existing[relative][2])):
                    stats["updated"] += 1
                    row_changed = True
                else:
                    stats["unchanged"] += 1
                    row_changed = False
                if row_changed:
                    conn.execute("DELETE FROM items WHERE id=? OR path=?", (item["id"], relative))
                    conn.execute(
                        "INSERT INTO items(id,kind,scope,project,owner,status,path,title,body,body_hash,"
                        "confidence,importance,usage,created,updated,last_used) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (item["id"], item["kind"], item["scope"], item["project"], item["owner"],
                         item["status"], item["path"], item["title"], item["body"], item["body_hash"],
                         item["confidence"], item["importance"], item["usage"], item["created"],
                         item["updated"], item["last_used"]))
                    conn.execute("DELETE FROM edges WHERE src=?", (item["id"],))
                    for edge in item["edges"]:
                        conn.execute("INSERT OR REPLACE INTO edges(src,dst,type) VALUES(?,?,?)",
                                     (item["id"], edge["to"], edge["type"]))
                        stats["edges"] += 1
                    if fts:
                        conn.execute("DELETE FROM items_fts WHERE id=?", (item["id"],))
                        conn.execute("INSERT INTO items_fts(id,title,body) VALUES(?,?,?)",
                                     (item["id"], item["title"], item["body"]))
                _ensure_vector(conn, item)
            for relative in sorted(set(existing) - seen):
                row = conn.execute("SELECT id FROM items WHERE path=?", (relative,)).fetchone()
                if not row:
                    continue
                conn.execute("DELETE FROM items WHERE path=?", (relative,))
                conn.execute("DELETE FROM edges WHERE src=? OR dst=?", (row["id"], row["id"]))
                conn.execute("DELETE FROM vectors WHERE id=?", (row["id"],))
                if fts:
                    conn.execute("DELETE FROM items_fts WHERE id=?", (row["id"],))
                stats["removed"] += 1
            conn.execute("INSERT INTO meta(key,value) VALUES('rebuilt_at',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (store.now(),))
        count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        if verify:
            stats["verified"] = count == len(seen_ids)
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


def _terms(query):
    terms = set(re.findall(r"[a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", str(query or "").lower()))
    terms |= {term[index:index + 2] for term in list(terms)
              if re.fullmatch(r"[\u4e00-\u9fff]{3,}", term)
              for index in range(len(term) - 1)}
    return terms


def related(item_id, limit=5, conn=None):
    """1 跳关联：返回与 item_id 有边相连的条目（含方向与边类型）。"""
    owned = conn is None
    conn = conn or _connect()
    try:
        edges = conn.execute("SELECT src,dst,type FROM edges WHERE src=? OR dst=?",
                             (item_id, item_id)).fetchall()
        grouped = {}
        for edge in edges:
            other = edge["dst"] if edge["src"] == item_id else edge["src"]
            if other == item_id:
                continue
            grouped.setdefault(other, []).append({
                "type": edge["type"],
                "direction": "out" if edge["src"] == item_id else "in",
            })
        rows = []
        for other, info in grouped.items():
            row = conn.execute("SELECT * FROM items WHERE id=?", (other,)).fetchone()
            if not row:
                continue
            item = dict(row)
            item["edges"] = info
            item["_score"] = score.pin_score(item)
            rows.append(item)
        rows.sort(key=lambda item: item["_score"], reverse=True)
        return rows[:max(1, int(limit))]
    finally:
        if owned:
            conn.close()


def upsert_item(item, *, actor=None):
    """单条 upsert：面板编辑/晋升后立即同步行、边、FTS 与向量，不等全量重建。

    owner 写侧校验：不能把别人名下的行改写成自己的。库里已有同 id/path 的行、
    且其 owner 与本次写入身份不一致时抛 PermissionError，不做覆盖。`actor`
    缺省取条目自带 owner（文件同步这类内部调用）；只有显式声明身份时才具备
    跨 owner 的语义，非法覆盖一律拒绝而不是"只写不查"。
    """
    relative = str(item.get("path") or "")
    if os.path.isabs(relative):
        try:
            relative = os.path.relpath(relative, store.root()).replace("\\", "/")
        except ValueError:
            pass
    actor_id = str(actor or item.get("owner") or "main")
    conn = _connect()
    try:
        with conn:
            existing = conn.execute("SELECT owner FROM items WHERE id=? OR path=?",
                                    (item["id"], relative)).fetchone()
            if existing is not None and str(existing["owner"] or "main") != actor_id:
                raise PermissionError("不能覆盖其他 Agent 的记忆：owner=%s，写入身份=%s"
                                      % (existing["owner"] or "main", actor_id))
            conn.execute("DELETE FROM items WHERE id=? OR path=?",
                         (item["id"], relative))
            conn.execute(
                "INSERT INTO items(id,kind,scope,project,owner,status,path,title,body,body_hash,"
                "confidence,importance,pinned,usage,created,updated,last_used) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["id"], item.get("kind", "fact"), item.get("scope", "global"),
                 item.get("project", ""), item.get("owner") or "main",
                 item.get("status", "active"),
                 relative, item.get("title", ""), item.get("body", ""),
                 item.get("body_hash") or store.sha256_text(item.get("body", "")),
                 item.get("confidence", 0.5), item.get("importance", 0.5),
                 int(item.get("pinned") or 0), int(item.get("usage") or 0),
                 item.get("created", ""), item.get("updated", ""),
                 item.get("last_used", "")))
            conn.execute("DELETE FROM edges WHERE src=?", (item["id"],))
            for edge in item.get("edges") or []:
                conn.execute("INSERT OR REPLACE INTO edges(src,dst,type) VALUES(?,?,?)",
                             (item["id"], edge.get("to"), edge.get("type")))
            if _fts(conn):
                conn.execute("DELETE FROM items_fts WHERE id=?", (item["id"],))
                conn.execute("INSERT INTO items_fts(id,title,body) VALUES(?,?,?)",
                             (item["id"], item.get("title", ""), item.get("body", "")))
            _ensure_vector(conn, item)
    finally:
        conn.close()


def touch(item_id, usage, last_used):
    """同步检索/采纳后的复用计数（文件侧由 promote.touch 写回）。"""
    conn = _connect()
    try:
        with conn:
            conn.execute("UPDATE items SET usage=?, last_used=? WHERE id=?",
                         (int(usage or 0), str(last_used or ""), str(item_id)))
    finally:
        conn.close()


def set_edges(src, edges):
    """把某条目的出边同步到索引（不重建全表）。"""
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM edges WHERE src=?", (src,))
            for edge in edges or []:
                target = str(edge.get("to") or "").strip()
                kind = str(edge.get("type") or "").strip()
                if target and kind:
                    conn.execute("INSERT OR REPLACE INTO edges(src,dst,type) VALUES(?,?,?)",
                                 (src, target, kind))
    finally:
        conn.close()


def search(query="", limit=10, scope=None, project=None, owner=None):
    """关键词 + 可选语义向量检索，再对前 3 条做 1 跳关联扩展。

    owner 过滤只限制「agent 私有」条目：global/project 层是共享/项目级知识，
    对所有 Agent 可见（与 memory_search 的召回口径一致）。

    语义仅在 DOCMIND_MEMORY_EMBED / runtime memory_embed 开启且条目有向量时参与；
    任何嵌入故障都退化为关键词，不影响检索可用性。
    """
    terms = _terms(query)
    conn = _connect()
    try:
        sql = ("SELECT * FROM items WHERE (status='active' "
               "OR (scope='project' AND status='candidate'))")
        params = []
        if scope:
            sql += " AND scope=?"
            params.append(str(scope))
        if project:
            sql += " AND (scope='global' OR project=?)"
            params.append(str(project))
        if owner:
            sql += " AND (owner=? OR scope IN ('global','project'))"
            params.append(str(owner))
        rows = [dict(row) for row in conn.execute(sql, params)]
        qvec = _embed_one(query) if terms and _embed_enabled() else []
        vectors = {}
        if qvec:
            for row in rows:
                found = conn.execute("SELECT vec FROM vectors WHERE id=?",
                                     (row["id"],)).fetchone()
                if not found:
                    continue
                try:
                    vector = json.loads(found["vec"])
                except (TypeError, ValueError):
                    continue
                if len(vector) == len(qvec):
                    vectors[row["id"]] = vector
        if terms:
            ranked = []
            for row in rows:
                text = (str(row.get("title") or "") + " " + str(row.get("body") or "")).lower()
                hits = sum(1 for term in terms if term in text)
                keyword = hits / len(terms)
                semantic = _cosine(qvec, vectors.get(row["id"], [])) if qvec else 0.0
                if hits == 0 and semantic < 0.25:
                    continue
                row["_score"] = score.retrieval_score(row, keyword=keyword,
                                                      semantic=semantic)
                row["_semantic"] = round(semantic, 4)
                ranked.append(row)
            ranked.sort(key=lambda item: item["_score"], reverse=True)
            rows = ranked
        else:
            for row in rows:
                row["_score"] = score.pin_score(row)
            rows.sort(key=lambda item: item["_score"], reverse=True)
        rows = rows[:max(1, min(50, int(limit)))]
        seen = {row["id"] for row in rows}
        edges = [(edge["src"], edge["dst"])
                 for edge in conn.execute("SELECT src,dst FROM edges")]
        if edges:
            seeds = {row["id"]: float(row.get("_score") or 0)
                     for row in rows[:3] if float(row.get("_score") or 0) > 0}
            ppr = graph.personalized_pagerank(edges, seeds)
            if ppr:
                max_seed = max(seeds.values()) if seeds else 1.0
                max_ppr = max(ppr.values()) or 1.0
                top_seed = rows[0]["id"] if rows else ""
                extra = []
                for node, value in sorted(ppr.items(), key=lambda pair: pair[1],
                                          reverse=True):
                    if node in seen:
                        continue
                    found = conn.execute("SELECT * FROM items WHERE id=?", (node,)).fetchone()
                    if not found:
                        continue
                    item = dict(found)
                    if item.get("status") not in ("active", "candidate"):
                        continue
                    if owner and not (item.get("owner") == str(owner)
                                      or item.get("scope") in ("global", "project")):
                        continue
                    if item.get("scope") == "project" and item.get("status") != "candidate":
                        continue
                    if project and item.get("scope") != "global"                             and item.get("project") != str(project):
                        continue
                    item["_score"] = round(max_seed * 0.35 * (value / max_ppr), 4)
                    item["_graph"] = round(value / max_ppr, 4)
                    item["related_to"] = top_seed
                    extra.append(item)
                    seen.add(node)
                    if len(extra) >= 6:
                        break
                return rows + extra
        extra = []
        for row in rows[:3]:
            for neighbour in related(row["id"], 3, conn=conn):
                if neighbour["id"] in seen:
                    continue
                if owner and not (neighbour.get("owner") == str(owner)
                                  or neighbour.get("scope") in ("global", "project")):
                    continue
                if project and neighbour.get("scope") != "global"                         and neighbour.get("project") != str(project):
                    continue
                neighbour["_score"] = round(float(row.get("_score") or 0) * 0.5, 4)
                neighbour["related_to"] = row["id"]
                extra.append(neighbour)
                seen.add(neighbour["id"])
        return rows + extra[:6]
    finally:
        conn.close()
