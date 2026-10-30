"""旧版记忆迁移（P0-4）：只读导出，不删不改旧库；幂等，可重跑。

- 逐字 episode / checkpoint → archive/legacy（只读、不注入）
- 已蒸馏摘要 / fact → 共享层候选（低置信、不 pin，等回执与采纳）
- preference → archive/legacy/preferences.json
- experience 集合（若可用）→ 共享 lessons 候选
"""
from __future__ import annotations

import json
import os
import sqlite3

from . import promote, store

MARKER = ".migrated-p0-4.json"
DISTILLED = "模型报告（已蒸馏）"


def default_db():
    from config import STATE_ROOT, state_path
    return state_path("DOCMIND_MEMORY_DB", os.path.join(STATE_ROOT, ".docmind_memory.sqlite3"))


def _rows(conn, table):
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM " + table)]
    except sqlite3.Error:
        return []


def _write_jsonl(path, rows):
    store.ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _importance(row):
    try:
        return max(0.0, min(1.0, float(row.get("importance") or 0.5)))
    except (TypeError, ValueError):
        return 0.5


def _migrate_experience(result):
    try:
        from config import EXPERIENCE_COLLECTION_NAME
        from vectorstore import get_collection
        collection = get_collection(EXPERIENCE_COLLECTION_NAME)
        data = collection.get(include=["documents", "metadatas"])
        documents = data.get("documents") or []
        metadatas = data.get("metadatas") or []
        for index, document in enumerate(documents):
            meta = metadatas[index] if index < len(metadatas) else {}
            meta = meta if isinstance(meta, dict) else {}
            text = str(meta.get("lesson") or document or "").strip()
            if not text:
                continue
            if promote.seed_shared(
                    "lesson", text, importance=0.5, confidence=0.4,
                    meta={"migrated_from": "experience",
                          "source_project": str(meta.get("project_id") or ""),
                          "outcome": str(meta.get("outcome") or "")}):
                result["experience"] += 1
    except Exception as exc:  # noqa: BLE001 —— 经验库不可用不阻断迁移
        result["experience_error"] = str(exc)[:200]


def run(db_path=None, force=False):
    db_path = db_path or default_db()
    marker_path = store.safe_path(MARKER)
    if os.path.exists(marker_path) and not force:
        return {"ok": True, "skipped": True, "marker": marker_path}
    result = {"ok": True, "db": db_path, "archived": 0, "seeded_facts": 0,
              "seeded_lessons": 0, "preferences": 0, "checkpoints": 0, "experience": 0,
              "retired": 0}
    if not os.path.exists(db_path):
        result.update({"ok": False, "error": "旧库不存在"})
        return result
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        memories = _rows(connection, "memories")
        checkpoints = _rows(connection, "checkpoints")
    finally:
        connection.close()

    archive = store.safe_path("archive", "legacy")
    store.ensure_dir(archive)
    archived_rows = []
    preferences = []
    for row in memories:
        item = {key: row.get(key) for key in
                ("scope", "id", "kind", "title", "content", "source", "status",
                 "backed", "importance", "updated")}
        archived_rows.append(item)
        kind = str(row.get("kind") or "")
        content = str(row.get("content") or "").strip()
        status = str(row.get("status") or "active")
        if status == "retired":
            result["retired"] += 1
        if kind == "preference":
            preferences.append(item)
            continue
        if not content:
            continue
        if kind == "fact" and status != "retired":
            promote.seed_shared("fact", content, importance=_importance(row), confidence=0.6,
                                meta={"migrated_from": "fact", "source_id": str(row.get("id") or "")})
            result["seeded_facts"] += 1
        elif content.startswith(DISTILLED):
            text = content[len(DISTILLED):].lstrip("：: ").strip()
            if text and promote.seed_shared(
                    "fact", text, importance=_importance(row), confidence=0.4,
                    meta={"migrated_from": "distilled", "source_id": str(row.get("id") or "")}):
                result["seeded_facts"] += 1

    _write_jsonl(os.path.join(archive, "episodes.jsonl"), archived_rows)
    result["archived"] = len(archived_rows)
    with open(os.path.join(archive, "preferences.json"), "w", encoding="utf-8") as handle:
        json.dump(preferences, handle, ensure_ascii=False, indent=2)
    result["preferences"] = len(preferences)

    checkpoint_rows = [{
        "id": row.get("id"), "session": row.get("session"), "question": row.get("question"),
        "status": row.get("status"), "reason": row.get("reason"),
        "answer": row.get("answer"), "updated": row.get("updated"),
        "journal_chars": len(str(row.get("journal") or "")),
    } for row in checkpoints]
    _write_jsonl(os.path.join(archive, "checkpoints.jsonl"), checkpoint_rows)
    result["checkpoints"] = len(checkpoint_rows)

    _migrate_experience(result)

    marker = {"schema_version": 1, "ts": store.now(), "db": db_path, "result": result}
    store.atomic_write(marker_path, json.dumps(marker, ensure_ascii=False, indent=2) + "\n")
    store.ledger("migration.p0_4", **{key: value for key, value in result.items()
                                      if isinstance(value, (int, str))})
    return result
