"""晋升门：项目候选 → 全局共享。

硬条件三条：①有通过的测试/验收/合并回执；②有采纳或复用信号；③通用化通过
（无密钥、绝对路径、项目标识、强上下文指代）。任一不满足就留在项目层。
晋升后的条目才可能被 pin（每轮注入）；其余只在需要时检索。
"""
from __future__ import annotations

import glob
import os
import re

from . import feedback, receipts, score, store

MAX_TEXT = 600
MIN_TEXT = 12
_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|token|secret|password|passwd|authorization)"
    r"\s*[:=]\s*[^\s,;\n]+|sk-[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|Bearer\s+[\w.\-/+=]+")
_ABS_PATH = re.compile(r"(?:[A-Za-z]:\\[^\s\"'<>|]+|/(?:home|Users|var|tmp|opt|mnt|srv)/[^\s\"'<>|]+)")
_CONTEXT = re.compile(r"本项目|这个项目|当前项目|我们项目|该项目")
_PASS_TYPES = {"test", "verify", "merge"}


def _clamp(value, default=0.5):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _normalize_kind(kind):
    return "lesson" if str(kind or "").strip() == "lesson" else "fact"


def _dir(project_id, kind):
    base = store.safe_path("projects", store.safe_id(project_id))
    return os.path.join(base, "facts" if kind == "fact" else "lessons")


def shared_dir(kind):
    return store.safe_path("shared", "facts" if kind == "fact" else "lessons")


def item_id(kind, text):
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return "mem-" + store.sha256_text(_normalize_kind(kind) + "|" + normalized)[:16]


def _read(path):
    try:
        header, body = store.split_frontmatter(store.read_text(path))
    except (OSError, ValueError):
        return None
    if header is None:
        return None
    item = dict(header)
    item["body"] = str(body or "").strip()
    item["path"] = path
    return item


def _write(path, header, body):
    store.atomic_write(path, store.render_frontmatter(header, body))


def genericize(text, project_id=""):
    """脱敏并判断能否脱离本项目读懂；返回 (清理后文本, 拒绝原因列表)。"""
    raw = str(text or "").strip()
    if len(raw) < MIN_TEXT:
        return raw, ["内容太短，脱离上下文难以复用"]
    if _SECRET.search(raw):
        return raw, ["含密钥/令牌，禁止跨项目沉淀"]
    if project_id and str(project_id) in raw:
        return raw, ["含项目标识"]
    if _CONTEXT.search(raw):
        return raw, ["依赖本项目上下文（本项目/当前项目）"]
    cleaned = _ABS_PATH.sub("<path>", raw)
    if _ABS_PATH.search(cleaned):
        return cleaned, ["仍含绝对路径"]
    return cleaned[:MAX_TEXT], []


def create_candidate(project_id, kind, text, *, title="", importance=0.6,
                     source_turn="", receipt_ids=(), task=""):
    kind = _normalize_kind(kind)
    body = str(text or "").strip()[:MAX_TEXT]
    if not body:
        return None
    target_dir = _dir(project_id, kind)
    mid = item_id(kind, body)
    path = os.path.join(target_dir, mid + ".md")
    existing = _read(path) if os.path.exists(path) else None
    if existing:
        existing["_new"] = False
        return existing
    now = store.now()
    header = {
        "id": mid, "kind": kind, "scope": "project", "status": "candidate",
        "title": str(title or body.splitlines()[0])[:80],
        "confidence": 0.5, "importance": _clamp(importance), "usage": 0,
        "pinned": False, "source_turn": str(source_turn or ""),
        "receipts": [str(rid) for rid in (receipt_ids or [])][:20],
        "task": str(task or "")[:120], "promoted_to": "",
        "created": now, "updated": now,
    }
    _write(path, header, body)
    store.ledger("memory.candidate", id=mid, kind=kind, project=store.safe_id(project_id))
    header["body"] = body
    header["path"] = path
    header["_new"] = True
    return header


def seed_shared(kind, text, *, importance=0.5, confidence=0.4, title="", meta=None):
    """迁移/回填用：直接写共享候选（不 pin、无回执，等后续验证）。"""
    kind = _normalize_kind(kind)
    body = str(text or "").strip()[:MAX_TEXT]
    if not body:
        return None
    mid = item_id(kind, body)
    path = os.path.join(shared_dir(kind), mid + ".md")
    if os.path.exists(path):
        return mid
    now = store.now()
    header = {
        "id": mid, "kind": kind, "scope": "global", "status": "candidate",
        "title": str(title or body.splitlines()[0])[:80],
        "confidence": _clamp(confidence), "importance": _clamp(importance),
        "usage": 0, "pinned": False, "receipts": [], "promoted_from": "",
        "created": now, "updated": now,
    }
    for key, value in (meta or {}).items():
        if key not in header:
            header[key] = value
    _write(path, header, body)
    store.ledger("memory.seed_shared", id=mid, kind=kind)
    return mid


def _candidate_paths(project_id):
    paths = []
    for kind in ("fact", "lesson"):
        paths.extend(sorted(glob.glob(os.path.join(_dir(project_id, kind), "*.md"))))
    return paths


def candidates(project_id):
    return [item for item in (_read(path) for path in _candidate_paths(project_id)) if item]


def _has_pass_receipt(project_id, receipt_ids):
    for rid in receipt_ids or []:
        row = receipts.get(project_id, rid)
        if row and row.get("type") in _PASS_TYPES and row.get("result") == "pass":
            return True
    return False


def evaluate(project_id, item):
    reasons = []
    if not _has_pass_receipt(project_id, item.get("receipts") or []):
        reasons.append("缺通过的测试/验收/合并回执")
    adoption = (bool(item.get("adopted")) or int(item.get("usage") or 0) >= 2)
    task = str(item.get("task") or "").strip()
    if not adoption and task:
        adoption = feedback.max_stars(project_id, task) >= 4
    if not adoption:
        reasons.append("缺采纳/复用信号")
    if _clamp(item.get("importance")) < 0.5:
        reasons.append("重要度不足")
    cleaned, scrub_reasons = genericize(item.get("body") or "", project_id)
    reasons.extend(scrub_reasons)
    return {"ok": not reasons, "reasons": reasons, "text": cleaned}


def promote(project_id, candidate_id):
    item = next((row for row in candidates(project_id) if row.get("id") == candidate_id), None)
    if item is None:
        return {"ok": False, "reasons": ["未找到候选条目"]}
    if item.get("status") != "candidate":
        return {"ok": False, "reasons": ["候选状态不是 candidate"]}
    verdict = evaluate(project_id, item)
    if not verdict["ok"]:
        return {"ok": False, "reasons": verdict["reasons"]}
    kind = _normalize_kind(item.get("kind"))
    shared_path = os.path.join(shared_dir(kind), candidate_id + ".md")
    now = store.now()
    header = {
        "id": candidate_id, "kind": kind, "scope": "global", "status": "active",
        "title": str(item.get("title") or "")[:80], "confidence": 0.85,
        "importance": _clamp(item.get("importance")),
        "usage": int(item.get("usage") or 0), "pinned": False,
        "receipts": list(item.get("receipts") or []),
        "promoted_from": os.path.relpath(item["path"], store.root()).replace("\\", "/"),
        "promoted_at": now, "created": item.get("created") or now, "updated": now,
    }
    header["pinned"] = bool(header["confidence"] >= 0.8 and header["importance"] >= 0.7)
    _write(shared_path, header, verdict["text"])
    item["status"] = "promoted"
    item["promoted_to"] = os.path.relpath(shared_path, store.root()).replace("\\", "/")
    item["updated"] = now
    _write(item["path"], {key: value for key, value in item.items()
                          if key not in ("body", "path")}, item.get("body") or "")
    store.ledger("memory.promote", id=candidate_id, kind=kind,
                 project=store.safe_id(project_id), pinned=header["pinned"])
    header["body"] = verdict["text"]
    header["path"] = shared_path
    return {"ok": True, "item": header, "pinned": header["pinned"]}


def sweep(project_id):
    checked = promoted = 0
    for item in candidates(project_id):
        if item.get("status") != "candidate":
            continue
        checked += 1
        try:
            result = promote(project_id, item.get("id") or "")
        except Exception:  # noqa: BLE001 —— 单条失败不阻断扫描
            continue
        if result.get("ok"):
            promoted += 1
    return {"checked": checked, "promoted": promoted}


def shared_items():
    rows = []
    for kind in ("fact", "lesson"):
        for path in sorted(glob.glob(os.path.join(shared_dir(kind), "*.md"))):
            item = _read(path)
            if item and item.get("status") == "active":
                item["kind"] = _normalize_kind(item.get("kind") or kind)
                rows.append(item)
    return rows


def pinned_shared(limit=5):
    rows = [item for item in shared_items() if item.get("pinned")]
    rows.sort(key=score.pin_score, reverse=True)
    return rows[:max(1, int(limit))]


def set_shared_pinned(item_id_value, pinned):
    for kind in ("fact", "lesson"):
        path = os.path.join(shared_dir(kind), str(item_id_value) + ".md")
        item = _read(path)
        if not item:
            continue
        item["pinned"] = bool(pinned)
        item["updated"] = store.now()
        _write(path, {key: value for key, value in item.items()
                      if key not in ("body", "path")}, item.get("body") or "")
        store.ledger("memory.pin", id=item_id_value, pinned=bool(pinned))
        return item
    return None


def shared_item(item_id_value):
    for kind in ("fact", "lesson"):
        item = _read(os.path.join(shared_dir(kind), str(item_id_value) + ".md"))
        if item:
            item["kind"] = _normalize_kind(item.get("kind") or kind)
            return item
    return None


def secret_hit(text):
    return bool(_SECRET.search(str(text or "")))


def edit_shared(item_id_value, *, text=None, pinned=None):
    """用户直接编辑共享条目：标 user_edit、清空回执、置信回 0.5（编辑即未验证）。"""
    item = shared_item(item_id_value)
    if item is None:
        return None
    now = store.now()
    if text is not None:
        body = str(text).strip()[:MAX_TEXT]
        if not body:
            raise ValueError("正文不能为空")
        if secret_hit(body):
            raise ValueError("内容含密钥/令牌，未保存")
        item["body"] = body
        item["source"] = "user_edit"
        item["receipts"] = []
        item["confidence"] = 0.5
        item["updated"] = now
    if pinned is not None:
        item["pinned"] = bool(pinned)
        item["updated"] = now
    _write(item["path"], {key: value for key, value in item.items()
                          if key not in ("body", "path")}, item["body"])
    store.ledger("memory.edit_shared", id=item_id_value, pinned=item.get("pinned"))
    return item


def delete_shared(item_id_value):
    item = shared_item(item_id_value)
    if item is None:
        return False
    os.remove(item["path"])
    store.ledger("memory.delete_shared", id=item_id_value, kind=item.get("kind"))
    return True
