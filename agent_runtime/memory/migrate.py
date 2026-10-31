"""旧版记忆迁移（P0-4）：只读导出，不删不改旧库；幂等，可重跑。

- 逐字 episode / checkpoint → archive/legacy（只读、不注入）
- 已蒸馏摘要 / fact → 共享层候选（低置信、不 pin，等回执与采纳）
- preference → archive/legacy/preferences.json
- experience 集合（若可用）→ 共享 lessons 候选
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

from . import profile, promote, store

MARKER = ".migrated-p0-4.json"
PROFILE_MARKER = ".profile-backfilled.json"
DISTILLED = "模型报告（已蒸馏）"
_PATH_ARG = re.compile(
    r"(?m)^\s*(?:path|file|target|source|dest|new_path|old_path)\s*:\s*[\"']?([^\"'\n]+)")


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


def _project_of(row):
    raw = str(row.get("scope") or "").strip()
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if isinstance(value, list) and value:
        raw = str(value[0])
    elif isinstance(value, dict):
        raw = str(value.get("project") or "")
    project = raw.strip()
    return project if project and project not in ("legacy", "default") else "default"


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
            source_project = str(meta.get("project_id") or "default")
            if promote.seed_project(
                    source_project, "lesson", text, importance=0.5, confidence=0.4,
                    meta={"migrated_from": "experience",
                          "source_project": source_project,
                          "outcome": str(meta.get("outcome") or "")}):
                result["experience"] += 1
                result["seeded_lessons"] += 1
    except Exception as exc:  # noqa: BLE001 —— 经验库不可用不阻断迁移
        result["experience_error"] = str(exc)[:200]


def _json_value(raw, fallback):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(str(raw or ""))
    except ValueError:
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _profile_patch(legacy, preference_rows, current):
    legacy = legacy if isinstance(legacy, dict) else {}
    current = current if isinstance(current, dict) else {}
    patch = {}
    for key in ("display_name", "language", "timezone", "location", "notes"):
        value = str(legacy.get(key) or "").strip()
        if value and not current.get(key):
            patch[key] = value
    prefs = dict(current.get("preferences") or {})
    for key, value in _json_value(legacy.get("preferences"), {}).items():
        prefs.setdefault(key, value)
    for row in preference_rows or ():
        title = str(row.get("title") or "").strip()
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        if title in ("language", "语言") or "沟通" in content:
            language = ""
            if "中文" in content:
                language = "中文"
            elif "英文" in content or "英语" in content:
                language = "英文"
            if language and not (current.get("language") or patch.get("language")):
                patch["language"] = language
        elif title in ("detail", "详细度") or "回答" in content:
            if "简洁" in content:
                prefs["detail"] = "简洁"
            elif "详细" in content:
                prefs["detail"] = "详细"
        elif title:
            prefs.setdefault(title, content)
    if prefs != (current.get("preferences") or {}):
        patch["preferences"] = prefs
    goals = _json_value(legacy.get("goals"), [])
    if goals:
        merged = list(current.get("goals") or [])
        for goal in goals:
            if goal and goal not in merged:
                merged.append(goal)
        if merged != (current.get("goals") or []):
            patch["goals"] = merged
    return patch


def _journal_records(rows):
    records = []
    for row in rows or ():
        journal = row.get("journal")
        if isinstance(journal, str):
            try:
                journal = json.loads(journal)
            except ValueError:
                journal = {}
        if not isinstance(journal, dict):
            journal = {}
        tools, paths = [], []
        for entry in (journal.get("executed") or []):
            if isinstance(entry, (list, tuple)):
                if not entry:
                    continue
                tools.append(str(entry[0]))
                args = str(entry[1]) if len(entry) > 1 else ""
                for match in _PATH_ARG.finditer(args):
                    value = match.group(1).strip().strip("'\"")
                    if value:
                        paths.append(value)
            elif isinstance(entry, dict):
                tool = entry.get("name") or entry.get("tool")
                if tool:
                    tools.append(str(tool))
                value = entry.get("path") or entry.get("file")
                if value:
                    paths.append(str(value))
        for change in (journal.get("file_changes") or []):
            if isinstance(change, dict):
                value = change.get("path") or change.get("file")
            else:
                value = change
            if value:
                paths.append(str(value))
        records.append({"question": str(row.get("question") or ""),
                        "paths": paths, "tools": tools})
    return records


def backfill_profile(db_path=None, force=False):
    """把旧库的显式画像/偏好与历史回合学习信号回填到 L1。

    旧迁移只归档 preference，没把它接到画像；实时 learning 又只统计开关打开
    之后的回合，导致升级后画像看起来全空。这里用可复现的确定性计数补齐：
    声明字段只搬旧库里已有的值，风格/扩展名/工具 domain 来自历史 checkpoint
    的 journal，绝不推断隐藏属性。
    """
    db_path = db_path or default_db()
    marker_path = store.safe_path(PROFILE_MARKER)
    if os.path.exists(marker_path) and not force:
        return {"ok": True, "skipped": True, "marker": marker_path}
    result = {"ok": True, "db": db_path, "declared": 0, "learned_records": 0}
    if not os.path.exists(db_path):
        result.update({"ok": False, "error": "旧库不存在"})
        return result
    connection = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        legacy = (_rows(connection, "user_profiles") or [{}])[0]
        preference_rows = [row for row in _rows(connection, "memories")
                           if str(row.get("kind") or "") == "preference"]
        checkpoints = _rows(connection, "checkpoints")
    finally:
        connection.close()

    patch = _profile_patch(legacy, preference_rows, profile.load())
    if patch:
        try:
            profile.save(patch)
            result["declared"] = len(patch)
        except Exception as exc:  # noqa: BLE001 —— 画像不可写不阻断迁移
            result["declared_error"] = str(exc)[:200]

    records = _journal_records(checkpoints)
    if records:
        try:
            import agent_memory
            agent_memory.learn_habits_bulk(records)
            result["learned_records"] = len(records)
        except Exception as exc:  # noqa: BLE001 —— 学习回填失败不阻断迁移
            result["learned_error"] = str(exc)[:200]
    try:
        data = profile.load()
        result["language"] = data.get("language") or ""
        result["learned"] = data.get("learned") or {}
    except Exception:  # noqa: BLE001
        pass

    store.atomic_write(marker_path, json.dumps(
        {"schema_version": 1, "ts": store.now(), "db": db_path,
         "result": {key: value for key, value in result.items()
                    if key in ("declared", "learned_records", "language")}},
        ensure_ascii=False, indent=2) + "\n")
    store.ledger("profile.backfill", declared=result["declared"],
                 learned_records=result["learned_records"])
    return result


def run(db_path=None, force=False):
    db_path = db_path or default_db()
    profile_result = backfill_profile(db_path)
    marker_path = store.safe_path(MARKER)
    if os.path.exists(marker_path) and not force:
        return {"ok": True, "skipped": True, "marker": marker_path,
                "profile": profile_result}
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
            promote.seed_project(_project_of(row), "fact", content,
                                 importance=_importance(row), confidence=0.6,
                                 meta={"migrated_from": "fact",
                                       "source_id": str(row.get("id") or "")})
            result["seeded_facts"] += 1
        elif content.startswith(DISTILLED):
            text = content[len(DISTILLED):].lstrip("：: ").strip()
            if text and promote.seed_project(
                    _project_of(row), "fact", text,
                    importance=_importance(row), confidence=0.4,
                    meta={"migrated_from": "distilled",
                          "source_id": str(row.get("id") or "")}):
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
    result["profile"] = profile_result

    marker = {"schema_version": 1, "ts": store.now(), "db": db_path, "result": result}
    store.atomic_write(marker_path, json.dumps(marker, ensure_ascii=False, indent=2) + "\n")
    store.ledger("migration.p0_4", **{key: value for key, value in result.items()
                                      if isinstance(value, (int, str))})
    return result
