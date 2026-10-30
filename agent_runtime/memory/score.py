"""L2 条目评分：常驻 pin 只看重要度/置信/复用/时效。

语义相似与关键词命中只在"按需检索"时叠加；pin 是每轮都注入的固定成本，
因此不能靠查询词相关度决定，否则同一批条目会被反复自我强化。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


def _clamp(value, default=0.5):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _recency(last, now):
    text = str(last or "").strip()
    if not text:
        return 0.5
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        days = max(0.0, ((now or datetime.now(timezone.utc)) - stamp).total_seconds() / 86400)
        return math.exp(-days / 30.0)
    except (TypeError, ValueError):
        return 0.5


def pin_score(item, now=None):
    """0.45·重要度 + 0.30·置信 + 0.15·复用 + 0.10·时效；越用越新会加分。"""
    usage = max(0, int(item.get("usage") or 0))
    usage_score = min(1.0, math.log1p(usage) / math.log1p(10))
    recency = _recency(item.get("last_used") or item.get("updated"), now)
    return round(0.45 * _clamp(item.get("importance")) +
                 0.30 * _clamp(item.get("confidence")) +
                 0.15 * usage_score +
                 0.10 * recency, 4)
