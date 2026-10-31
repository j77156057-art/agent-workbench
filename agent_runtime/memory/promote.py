"""晋升门：项目候选 → 全局共享。

硬条件三条：①有通过的测试/验收/合并回执；②有采纳或复用信号；③通用化通过
（无密钥、绝对路径、项目标识、强上下文指代）。任一不满足就留在项目层。
晋升后的条目才可能被 pin（每轮注入）；其余只在需要时检索。
"""
from __future__ import annotations

import glob
import os
import re
from difflib import SequenceMatcher

from . import feedback, receipts, score, store

MAX_TEXT = 600
MIN_TEXT = 12
_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|token|secret|password|passwd|authorization)"
    r"\s*[:=]\s*[^\s,;\n]+|sk-[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|Bearer\s+[\w.\-/+=]+")
_ABS_PATH = re.compile(r"(?:[A-Za-z]:\\[^\s\"'<>|]+|/(?:home|Users|var|tmp|opt|mnt|srv)/[^\s\"'<>|]+)")
_CONTEXT = re.compile(r"本项目|这个项目|当前项目|我们项目|该项目")
_PASS_TYPES = {"test", "verify", "merge", "review"}
EDGE_TYPES = {"supersedes", "derived_from", "used_in", "refines", "contradicts"}


def _clamp(value, default=0.5):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _grams(text):
    raw = re.sub(r"\s+", "", str(text or "").lower())
    tokens = set(re.findall(r"[a-z0-9_]+", raw))
    for chunk in re.findall(r"[一-鿿]+", raw):
        if len(chunk) <= 2:
            tokens.add(chunk)
        else:
            tokens.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return tokens


def text_similarity(a, b):
    """轻量确定性相似度：中文 bigram + 英文词的 Jaccard；短文本更保守。"""
    na = re.sub(r"\s+", "", str(a or "").lower())
    nb = re.sub(r"\s+", "", str(b or "").lower())
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    grams_a, grams_b = _grams(na), _grams(nb)
    if not grams_a or not grams_b:
        return 0.0
    score = len(grams_a & grams_b) / len(grams_a | grams_b)
    if len(na) < 24 or len(nb) < 24:
        if na in nb or nb in na:
            return max(score, 0.9)
        # 短句用字符级相似度：一词之差应算重复，主题相近但选项不同不算。
        return max(score, SequenceMatcher(None, na, nb).ratio())
    return score


def find_similar(project_id, kind, text, *, threshold=0.88):
    """在共享 active 与项目候选中找近似条目；共享优先（同分）。"""
    kind = _normalize_kind(kind)
    best_score, best_item = 0.0, None
    for item in shared_items() + candidates(project_id):
        if _normalize_kind(item.get("kind")) != kind:
            continue
        if item.get("scope") == "project" and item.get("status") != "candidate":
            continue
        score = text_similarity(text, item.get("body"))
        if score > best_score:
            best_score, best_item = score, item
    if best_item is None or best_score < threshold:
        return 0.0, None
    return best_score, best_item


def _merge_into(item, body, importance, source_turn, receipt_ids, task):
    now = store.now()
    merged = dict(item)
    merged["receipts"] = list(dict.fromkeys(
        list(item.get("receipts") or []) + list(receipt_ids or [])))[:20]
    merged["importance"] = max(_clamp(item.get("importance")),
                               _clamp(importance))
    merged["usage"] = int(item.get("usage") or 0) + 1
    merged["last_used"] = now
    merged["updated"] = now
    if merged.get("scope") == "project":
        turns = [str(merged.get("source_turn") or ""), str(source_turn or "")]
        merged["source_turn"] = ";".join(dict.fromkeys(item for item in turns if item))[:200]
    if task and not merged.get("task"):
        merged["task"] = str(task)[:120]
    _write(merged["path"], {key: value for key, value in merged.items()
                            if key not in ("body", "path")}, merged.get("body") or "")
    _sync_index(merged)
    store.ledger("memory.merge", id=merged.get("id"),
                 into_scope=merged.get("scope"), usage=merged["usage"])
    merged["_new"] = False
    merged["_merged"] = True
    return merged


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


def project_item_id(project_id, kind, text):
    """项目候选 id 带项目前缀，避免同一文案在两个项目互相覆盖索引。"""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    seed = store.safe_id(project_id or "default") + "|" + _normalize_kind(kind) + "|" + normalized
    return "mem-" + store.sha256_text(seed)[:16]


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


def _clean_edges(edges):
    out = []
    seen = set()
    for raw in edges or []:
        if not isinstance(raw, dict):
            continue
        target = str(raw.get("to") or raw.get("dst") or "").strip()[:80]
        kind = str(raw.get("type") or "").strip()
        if not target or kind not in EDGE_TYPES or (kind, target) in seen:
            continue
        seen.add((kind, target))
        out.append({"type": kind, "to": target})
    return out[:20]


def create_candidate(project_id, kind, text, *, title="", importance=0.6,
                     source_turn="", receipt_ids=(), task="", edges=None):
    kind = _normalize_kind(kind)
    body = str(text or "").strip()[:MAX_TEXT]
    if not body:
        return None
    target_dir = _dir(project_id, kind)
    mid = project_item_id(project_id, kind, body)
    path = os.path.join(target_dir, mid + ".md")
    existing = _read(path) if os.path.exists(path) else None
    if existing:
        existing["_new"] = False
        return existing
    _score, similar = find_similar(project_id, kind, body)
    if similar is not None:
        return _merge_into(similar, body, importance, source_turn, receipt_ids, task)
    now = store.now()
    header = {
        "id": mid, "kind": kind, "scope": "project", "status": "candidate",
        "title": str(title or body.splitlines()[0])[:80],
        "confidence": 0.5, "importance": _clamp(importance), "usage": 0,
        "pinned": False, "source_turn": str(source_turn or ""),
        "receipts": [str(rid) for rid in (receipt_ids or [])][:20],
        "task": str(task or "")[:120], "promoted_to": "",
        "edges": _clean_edges(edges), "created": now, "updated": now,
    }
    _write(path, header, body)
    store.ledger("memory.candidate", id=mid, kind=kind, project=store.safe_id(project_id))
    header["body"] = body
    header["path"] = path
    header["_new"] = True
    return header


def seed_project(project_id, kind, text, *, importance=0.5, confidence=0.4,
                 title="", meta=None):
    """迁移用：按原 scope 落项目候选，保留项目隔离；晋升仍需门。"""
    kind = _normalize_kind(kind)
    body = str(text or "").strip()[:MAX_TEXT]
    if not body:
        return None
    mid = project_item_id(project_id, kind, body)
    path = os.path.join(_dir(project_id, kind), mid + ".md")
    if os.path.exists(path):
        return mid
    now = store.now()
    header = {
        "id": mid, "kind": kind, "scope": "project", "status": "candidate",
        "title": str(title or body.splitlines()[0])[:80],
        "confidence": _clamp(confidence), "importance": _clamp(importance),
        "usage": 0, "pinned": False, "receipts": [], "promoted_to": "",
        "created": now, "updated": now,
    }
    for key, value in (meta or {}).items():
        if key not in header:
            header[key] = value
    _write(path, header, body)
    store.ledger("memory.seed_project", id=mid, kind=kind,
                 project=store.safe_id(project_id))
    return mid


def seed_shared(kind, text, *, importance=0.5, confidence=0.4, title="", meta=None,
                status="candidate"):
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
        "id": mid, "kind": kind,
        "status": status if status in ("candidate", "active") else "candidate",
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
    if item.get("promotion_frozen"):
        reasons.append("用户低评分冻结晋升")
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
        "edges": _clean_edges(item.get("edges") or []),
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
    _sync_index(header)
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
    _sync_index(item)
    store.ledger("memory.edit_shared", id=item_id_value, pinned=item.get("pinned"))
    return item


def delete_shared(item_id_value):
    item = shared_item(item_id_value)
    if item is None:
        return False
    os.remove(item["path"])
    store.ledger("memory.delete_shared", id=item_id_value, kind=item.get("kind"))
    return True


def touch(item_ids, project_id=""):
    """检索/采纳后的复用计数：写回文件并同步索引 usage（pin 排序会用到）。"""
    now = store.now()
    touched = 0
    for item_id_value in item_ids or []:
        item, _scope = _find_item(item_id_value, project_id)
        if item is None:
            continue
        item["usage"] = int(item.get("usage") or 0) + 1
        item["last_used"] = now
        item["updated"] = now
        _write(item["path"], {key: value for key, value in item.items()
                              if key not in ("body", "path")}, item.get("body") or "")
        try:
            from . import index
            index.touch(item_id_value, item["usage"], now)
        except Exception:  # noqa: BLE001
            pass
        touched += 1
    return touched


def adopt(project_id, candidate_id):
    """用户采纳项目候选：生成 review 回执、记采纳、尝试晋升。"""
    item = next((row for row in candidates(project_id)
                 if row.get("id") == candidate_id and row.get("status") == "candidate"), None)
    if item is None:
        return {"ok": False, "reasons": ["未找到待晋升候选"]}
    now = store.now()
    receipt_id = receipts.record(project_id, type="review", result="pass",
                                 cmd="user_review", evidence="user adopted candidate",
                                 detail=str(candidate_id))
    item["receipts"] = list(dict.fromkeys(list(item.get("receipts") or []) + [receipt_id]))[:20]
    item["adopted"] = True
    item["promotion_frozen"] = False
    item["usage"] = int(item.get("usage") or 0) + 1
    item["last_used"] = now
    item["updated"] = now
    _write(item["path"], {key: value for key, value in item.items()
                          if key not in ("body", "path")}, item.get("body") or "")
    _sync_index(item)
    result = promote(project_id, candidate_id)
    if not result.get("ok"):
        return {"ok": True, "promoted": False,
                "reasons": result.get("reasons") or [], "item": item}
    return {"ok": True, "promoted": True, "item": result.get("item"),
            "pinned": result.get("pinned")}


def apply_feedback(project_id, task, stars):
    """把任务级星级归因到该任务的候选：4–5 星采纳，1–2 星冻结。"""
    task = str(task or "").strip()
    if not task:
        return {"applied": 0, "promoted": 0}
    try:
        score = int(stars)
    except (TypeError, ValueError):
        return {"applied": 0, "promoted": 0}
    affected = 0
    for item in candidates(project_id):
        if item.get("status") != "candidate" or str(item.get("task") or "") != task:
            continue
        affected += 1
        now = store.now()
        if score >= 4:
            receipt_id = receipts.record(project_id, type="review", result="pass",
                                         cmd="user_rating", evidence="task rated %d stars" % score,
                                         detail=str(item.get("id") or ""))
            item["receipts"] = list(dict.fromkeys(
                list(item.get("receipts") or []) + [receipt_id]))[:20]
            item["adopted"] = True
            item["promotion_frozen"] = False
            item["usage"] = int(item.get("usage") or 0) + 1
            item["last_used"] = now
        elif score <= 2:
            item["promotion_frozen"] = True
            item["confidence"] = min(float(item.get("confidence") or 0.5), 0.3)
        item["updated"] = now
        _write(item["path"], {key: value for key, value in item.items()
                              if key not in ("body", "path")}, item.get("body") or "")
        _sync_index(item)
    promoted = sweep(project_id).get("promoted", 0) if score >= 4 else 0
    store.ledger("memory.feedback", task=task, stars=score, applied=affected,
                 promoted=promoted)
    return {"applied": affected, "promoted": promoted}


def _find_item(item_id_value, project_id=""):
    item = shared_item(item_id_value)
    if item:
        return item, "global"
    if project_id:
        for kind in ("fact", "lesson"):
            path = os.path.join(_dir(project_id, kind), str(item_id_value) + ".md")
            item = _read(path)
            if item:
                item["kind"] = kind
                return item, "project"
    return None, ""


def link(item_id_value, target, edge_type, project_id=""):
    """给条目加一条类型化边（supersedes / derived_from / used_in / refines / contradicts）。"""
    edge_type = str(edge_type or "").strip()
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"未知关联类型: {edge_type}")
    target = str(target or "").strip()[:80]
    if not target:
        raise ValueError("关联目标不能为空")
    if target == str(item_id_value):
        raise ValueError("不能关联自己")
    item, _scope = _find_item(item_id_value, project_id)
    if item is None:
        return None
    edges = _clean_edges(item.get("edges") or [])
    if not any(edge["type"] == edge_type and edge["to"] == target for edge in edges):
        edges.append({"type": edge_type, "to": target})
    item["edges"] = edges
    item["updated"] = store.now()
    _write(item["path"], {key: value for key, value in item.items()
                          if key not in ("body", "path")}, item.get("body") or "")
    try:
        from . import index
        index.set_edges(item_id_value, edges)
    except Exception:  # noqa: BLE001 —— 索引更新失败不阻断文件事实
        pass
    store.ledger("memory.link", id=item_id_value, dst=target, type=edge_type)
    return item


def unlink(item_id_value, target, edge_type=""):
    item, _scope = _find_item(item_id_value)
    if item is None:
        return None
    edges = [edge for edge in _clean_edges(item.get("edges") or [])
             if not (edge["to"] == str(target)
                     and (not edge_type or edge["type"] == str(edge_type)))]
    item["edges"] = edges
    item["updated"] = store.now()
    _write(item["path"], {key: value for key, value in item.items()
                          if key not in ("body", "path")}, item.get("body") or "")
    try:
        from . import index
        index.set_edges(item_id_value, edges)
    except Exception:  # noqa: BLE001
        pass
    store.ledger("memory.unlink", id=item_id_value, dst=target, type=edge_type or "")
    return item


def related(item_id_value, limit=5):
    from . import index
    return index.related(item_id_value, limit)


def retrievable(project_id, limit=30):
    """未注入但可按需检索的条目：共享层未 pin + 项目层待晋升候选。"""
    rows = []
    for item in shared_items():
        if item.get("pinned"):
            continue
        rows.append({"id": item.get("id"), "kind": item.get("kind"),
                     "scope": "global", "status": item.get("status"),
                     "title": item.get("title"), "body": item.get("body"),
                     "confidence": item.get("confidence"),
                     "importance": item.get("importance"),
                     "pinned": False,
                     "edges": len(_clean_edges(item.get("edges") or []))})
    for item in candidates(project_id):
        if item.get("status") != "candidate":
            continue
        rows.append({"id": item.get("id"), "kind": _normalize_kind(item.get("kind")),
                     "scope": "project", "status": item.get("status"),
                     "title": item.get("title"), "body": item.get("body"),
                     "confidence": item.get("confidence"),
                     "importance": item.get("importance"),
                     "pinned": False,
                     "edges": len(_clean_edges(item.get("edges") or []))})
    rows.sort(key=score.pin_score, reverse=True)
    return rows[:max(1, int(limit))]


def _sync_index(item):
    try:
        from . import index
        index.upsert_item(item)
    except Exception:  # noqa: BLE001 —— 索引同步失败不阻断文件事实
        pass


def dedupe_candidates(project_id, threshold=0.88):
    """对存量候选做一次近似合并（新写入已自动合并，这里清理历史碎片）。"""
    rows = [item for item in candidates(project_id) if item.get("status") == "candidate"]
    rows.sort(key=lambda item: str(item.get("created") or item.get("updated") or ""))
    kept = []
    merged = 0
    for item in rows:
        match = None
        for keeper in kept:
            if _normalize_kind(keeper.get("kind")) != _normalize_kind(item.get("kind")):
                continue
            if text_similarity(item.get("body"), keeper.get("body")) >= threshold:
                match = keeper
                break
        if match is None:
            kept.append(item)
            continue
        updated = _merge_into(match, item.get("body") or "",
                              item.get("importance") or 0.5,
                              item.get("source_turn") or "",
                              item.get("receipts") or [], item.get("task") or "")
        kept = [updated if keeper is match else keeper for keeper in kept]
        item["status"] = "merged"
        item["merged_into"] = match.get("id")
        item["updated"] = store.now()
        _write(item["path"], {key: value for key, value in item.items()
                              if key not in ("body", "path")}, item.get("body") or "")
        _sync_index(item)
        merged += 1
    store.ledger("memory.dedupe", project=store.safe_id(project_id),
                 checked=len(rows), merged=merged)
    return {"checked": len(rows), "merged": merged}
