"""记忆落盘底座：路径守卫、原子写、审计日志、frontmatter 解析。

文件是事实来源；`shared/index.sqlite3` 只是可重建的派生索引，删掉不丢数据。
所有写入都走 `atomic_write`（tmp + os.replace），避免多窗口读到半截文件。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone

import yaml

from config import STATE_ROOT, state_path

LEDGER_NAME = "ledger.jsonl"
_LOCK = threading.RLock()


def root():
    """记忆根目录；测试用 DOCMIND_MEMORY_DIR 指到临时目录。"""
    return state_path("DOCMIND_MEMORY_DIR", os.path.join(STATE_ROOT, ".docmind", "memory"))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_path(*parts):
    """拼接记忆根下的路径；任何越界（..、绝对路径、盘符）直接拒绝。"""
    base = os.path.abspath(root())
    target = os.path.abspath(os.path.join(base, *[str(p) for p in parts]))
    if os.path.commonpath([base, target]) != base:
        raise ValueError(f"记忆路径越界: {parts!r}")
    return target


def safe_id(value, fallback="default"):
    """把 session/project id 收敛成单层文件名；空值或全非法字符用 fallback。"""
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip(".-")[:80]
    return text or fallback


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def atomic_write(path, text):
    """原子写：同目录临时文件 + os.replace；失败不留半截文件。"""
    directory = os.path.dirname(os.path.abspath(path))
    ensure_dir(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mem-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(str(text))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def sha256_text(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def ledger(event, **fields):
    """追加一条审计记录；失败绝不阻断主流程，也绝不记录正文。"""
    try:
        path = safe_path(LEDGER_NAME)
        record = {"ts": now(), "event": str(event)}
        record.update({key: value for key, value in fields.items() if value is not None})
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with _LOCK:
            ensure_dir(os.path.dirname(path))
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


def split_frontmatter(text):
    """拆 YAML frontmatter：返回 (header, body)。

    无 frontmatter → (None, 原文)；frontmatter 损坏抛 ValueError（调用方决定跳过还是失败）。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.startswith("---\n"):
        return None, raw
    end = raw.find("\n---\n", 4)
    if end < 0:
        raise ValueError("frontmatter 未闭合")
    header_text = raw[4:end]
    body = raw[end + 5:]
    try:
        header = yaml.safe_load(header_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"frontmatter YAML 损坏: {exc}") from exc
    if header is None:
        header = {}
    if not isinstance(header, dict):
        raise ValueError("frontmatter 必须是键值对象")
    return header, body


def render_frontmatter(header, body):
    """渲染 frontmatter + 正文；键序稳定，便于 hash 与 diff。"""
    header_text = yaml.safe_dump(header, allow_unicode=True, sort_keys=False,
                                 default_flow_style=False).strip()
    return "---\n" + header_text + "\n---\n" + str(body or "")


def load_items(path):
    """读取 hooks.md / errors.md 这类 items 列表文件；缺失或损坏一律返回 []。"""
    try:
        if not os.path.exists(path):
            return []
        header, _ = split_frontmatter(read_text(path))
    except (OSError, ValueError):
        return []
    items = (header or {}).get("items") or []
    return [item for item in items if isinstance(item, dict)]


def save_items(path, items):
    atomic_write(path, render_frontmatter({"version": 1, "items": list(items)}, ""))
