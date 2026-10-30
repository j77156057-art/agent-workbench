"""系统回执：测试、验收、命令、合并、评审的不可变凭据。

模型不可写、不可改；任务书标 done 只认这里产出的回执。每条回执一个 JSON 文件，
写入即不可变（重复写只会得到新 id）。
"""
from __future__ import annotations

import glob
import json
import os
import uuid
from datetime import datetime, timezone

from . import store

TYPES = {"test", "verify", "command", "merge", "review"}
RESULTS = {"pass", "fail", "uncertain"}
MAX_TEXT = 2000


def _dir(project_id):
    return store.safe_path("projects", store.safe_id(project_id), "receipts")


def record(project_id, *, session="", task="", type="verify", result="uncertain",
           cmd="", evidence="", detail=""):
    kind = str(type or "verify").strip()
    outcome = str(result or "uncertain").strip()
    if kind not in TYPES:
        raise ValueError(f"未知回执类型: {kind}")
    if outcome not in RESULTS:
        raise ValueError(f"未知回执结果: {outcome}")
    now = datetime.now(timezone.utc)
    receipt_id = "rc-" + now.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    payload = {
        "schema_version": 1,
        "id": receipt_id,
        "ts": store.now(),
        "project": store.safe_id(project_id),
        "session": store.safe_id(session, ""),
        "task": str(task or "")[:120],
        "type": kind,
        "result": outcome,
        "cmd": str(cmd or "")[:MAX_TEXT],
        "evidence": str(evidence or "")[:MAX_TEXT],
        "detail": str(detail or "")[:MAX_TEXT],
    }
    target = os.path.join(_dir(project_id), receipt_id + ".json")
    store.atomic_write(target, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return receipt_id


def _valid_id(receipt_id):
    text = str(receipt_id or "")
    return text.startswith("rc-") and all(ch.isalnum() or ch in "-_" for ch in text)


def get(project_id, receipt_id):
    if not _valid_id(receipt_id):
        return None
    target = os.path.join(_dir(project_id), receipt_id + ".json")
    try:
        with open(target, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def list_receipts(project_id, limit=20, task=""):
    rows = []
    for target in glob.glob(os.path.join(_dir(project_id), "*.json")):
        try:
            with open(target, "r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if task and str(value.get("task") or "") != str(task):
            continue
        rows.append(value)
    rows.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
    return rows[:max(1, int(limit))]
