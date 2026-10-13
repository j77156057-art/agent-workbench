"""Agent 自身结构化日志：落项目状态目录、不破坏现有事件流模型。

## 为什么这个模块存在

项目采用**「事件流审计」**而非「传统 stdout 日志」的可观测设计（参见
`docs/cockpit-v2-redesign.md` §1.2 + 决策 8「每票即验」）：

- agent 的决策/工具调用/状态变化由 `project_state` / `lanes` / `verification` /
  `workflow_self_review` 等结构化事件承载
- Python `logging` 模块在整个 `agent_runtime/` 几乎不出现（仅 `mcp_autoconnect.py` 用过）
- 「agent 看自己」的诉求由 `output_audit.py` 审计工作流结果来满足

但事件流审计**不解决**一种需求：**当 agent 自身想读自己某次执行的原始 traceback
或工具内部状态时**——审计把它们结构化为 verdict，但 agent 不会主动
`traceback.format_exc()` 那种原始文本回看自己。

本模块就是补这个口子，提供一个**轻量、可选 opt-in** 的 JSON Lines 落盘：
- 文件名 `agent-log-v1.jsonl`，落在项目状态目录（与 `plan-v1.json` / `lanes.json` 同规矩）
- 每行一个事件：`{"ts": ..., "kind": ..., "session_id": ..., "turn_id": ..., "fields": {...}}`
- 写用 `threading.RLock` + 原子替换；读用 `read()` 反向 tail

## 与 `project_state.py` 的差异

`project_state.py` 落**单一文件 + 一次性迁移**（`plan-v1.json` 走 `_read/_write` 覆写）。
`agent_log.py` 落**JSONL 追加**（每行一个事件，append-only，不做迁移）—— 这是设计选择：
- 日志天然 append-only
- 一次性迁移在 log 这种持续流式写入的介质上没意义

## 使用示例

```python
from agent_runtime import agent_log
agent_log.event(root, "tool_call_failed", tool="dev_apply_edits", error=repr(exc))
events = agent_log.read(root, kind="tool_call_failed", limit=50)
```

## 不可退让的规矩

1. **写失败一律静默**——日志是辅助观测，绝不能反过来影响主流程（`try/except` 全包）
2. **不抛异常**——`event()` / `read()` 任一步失败返回 `None` 或 `[]`
3. **不污染工作树**——文件落 `project_state` 状态目录（`git status` 不因此变脏）
4. **不替代结构化事件**——这是「agent 看自己」补充，不与 `project_state` / `lanes` 抢主路径
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Mapping, Sequence

import project_state

_LOCK = threading.RLock()

#: 日志文件名（带版本号：将来换结构时老文件自然作废，避免新代码误读老格式）
LOG_FILE = "agent-log-v1.jsonl"

#: 单文件最大字节数（防止日志无限增长把磁盘写满）；超过会**截断**旧内容
MAX_BYTES = 4 * 1024 * 1024  # 4 MiB

#: 单次 read() 默认最多返回多少条（防止一次性把全文件读进内存）
DEFAULT_READ_LIMIT = 200

#: 敏感字段名前缀（写入时整段字段会替换为 `[REDACTED]`）—— 防止 agent 误把密钥写进日志
_SENSITIVE_KEY_PREFIXES = (
    "api_key", "apikey", "access_token", "password", "passwd",
    "secret", "private_key", "authorization",
)


def _scrub(value: Any) -> Any:
    """脱敏：dict 中带敏感前缀的字段值替换为 `[REDACTED]`。递归处理嵌套 dict；非 dict 原样返回。"""
    if not isinstance(value, Mapping):
        return value
    out: dict[str, Any] = {}
    for key, val in value.items():
        key_lower = str(key).lower()
        if any(key_lower.startswith(prefix) for prefix in _SENSITIVE_KEY_PREFIXES):
            out[key] = "[REDACTED]"
        elif isinstance(val, Mapping):
            out[key] = _scrub(val)
        else:
            out[key] = val
    return out


def _now() -> float:
    return time.time()


def _log_path(root: str) -> str | None:
    """解析日志文件路径。root 为空时返回 None（透传到 stderr 也不做）。"""
    if not root:
        return None
    try:
        return project_state.path(root, LOG_FILE)
    except (ValueError, OSError):
        return None


def _append_atomically(path: str, line: str) -> bool:
    """追加一行到 JSONL。原子写：先写 .tmp 再 os.replace。失败返回 False，不抛。"""
    try:
        with _LOCK:
            existing = ""
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as handle:
                        existing = handle.read()
                except OSError:
                    existing = ""
            # 超过 MAX_BYTES 时截断：保留后半（最近的事件）
            if len(existing.encode("utf-8")) + len(line.encode("utf-8")) > MAX_BYTES:
                tail = existing[-MAX_BYTES // 2:] if existing else ""
                existing = tail
            payload = existing + line
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, path)
        return True
    except (OSError, ValueError):
        return False


def event(root: str, kind: str, **fields: Any) -> bool:
    """记录一条结构化事件。

    :param root: 项目根目录（与 `project_state.path(root, ...)` 同款语义）
    :param kind: 事件类型（"agent_init" / "tool_call_failed" / "policy_decision" 等）
    :param fields: 任意键值对（dict 字段会脱敏，敏感前缀字段值替换为 `[REDACTED]`）
    :return: True 写入成功 / False 失败或 root 为空（**永不抛异常**）
    """
    if not kind:
        return False
    path = _log_path(root)
    if path is None:
        return False
    record = {
        "ts": _now(),
        "kind": str(kind),
        "fields": _scrub(fields),
    }
    try:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    except (TypeError, ValueError):
        return False
    return _append_atomically(path, line)


def read(
    root: str,
    *,
    kind: str | None = None,
    since_ts: float | None = None,
    limit: int = DEFAULT_READ_LIMIT,
) -> list[dict[str, Any]]:
    """读取事件记录（**反向 tail**——最近的在最前）。

    :param root: 项目根目录
    :param kind: 事件类型过滤（None = 不过滤）
    :param since_ts: 时间戳过滤（仅返回 ts >= since_ts 的事件，None = 不过滤）
    :param limit: 最多返回多少条
    :return: 事件列表（按时间倒序），root 为空 / 文件不存在 / 失败时返回 `[]`（**永不抛异常**）
    """
    if not root:
        return []
    if limit <= 0:
        return []
    path = _log_path(root)
    if path is None or not os.path.exists(path):
        return []
    try:
        with _LOCK:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                raw = handle.read()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    # 反向遍历行（每行一条 JSON 事件）
    for line in reversed(raw.splitlines()):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        if kind is not None and rec.get("kind") != kind:
            continue
        ts = rec.get("ts")
        if since_ts is not None:
            try:
                if ts is None or float(ts) < since_ts:
                    continue
            except (TypeError, ValueError):
                continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def tail_summary(root: str, *, limit: int = 20) -> Sequence[str]:
    """把最近 N 条事件压成单行字符串列表（给人看的紧凑版）。供 UI 调试面板用。

    失败 / 无事件 / root 为空时返回 `[]`。"""
    if not root:
        return []
    events = read(root, limit=limit)
    if not events:
        return []
    out: list[str] = []
    for rec in events:
        try:
            ts = float(rec.get("ts") or 0)
            kind = str(rec.get("kind") or "?")
            fields = rec.get("fields") or {}
            if not isinstance(fields, dict):
                fields = {}
            # 紧凑：只取前 5 个字段，避免一行过长
            compact = ", ".join(
                "%s=%s" % (k, v) for k, v in list(fields.items())[:5]
            )
            out.append("[%.3f] %s %s" % (ts, kind, compact))
        except (TypeError, ValueError):
            continue
    return out
