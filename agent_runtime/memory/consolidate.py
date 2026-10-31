"""实时固化：每轮结束异步抽取候选记忆。

结构性控制（不是预算）：单 worker、跳过平凡轮、失败只计量；模型可单独配置
（DOCMIND_MEMORY_MODEL，空 = 跟随主模型）。任何异常都不影响主流程。
"""
from __future__ import annotations

import atexit
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from config import get_memory_model

from . import episodes, metrics, promote
from .injection import enabled

MAX_SUMMARY = 1200
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-v2")
atexit.register(lambda: _EXECUTOR.shutdown(wait=False, cancel_futures=True))
_INFLIGHT = set()
_LOCK = threading.Lock()


def _trivial(episode):
    result = episode.get("result") if isinstance(episode.get("result"), dict) else {}
    status = str(result.get("status") or "")
    if status != "completed":
        return "status:" + (status or "unknown")
    summary = str(result.get("summary") or "").strip()
    if not summary:
        return "empty_summary"
    if re.search(r"尚未完成验证|不能确认任务成功|未通过验证", summary):
        return "unverified_turn"
    if not episode.get("actions") and len(summary) < 40:
        return "trivial_turn"
    return ""


def _extract_json(text):
    raw = str(text or "")
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:index + 1])
                except ValueError:
                    return None
    return None


def _tokens(usage):
    if not isinstance(usage, dict):
        return 0, 0
    return (int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            int(usage.get("completion_tokens") or usage.get("output_tokens") or 0))


def _client(llm):
    """空设置 → 主模型；显式模型只允许同 provider，避免拿错 Key/端点。"""
    target = get_memory_model()
    if not target:
        return llm, "follow"
    provider, sep, model = target.partition(":")
    if not sep:
        provider, model = "", target
    if provider and provider != getattr(llm, "provider", ""):
        return llm, "fallback_provider"
    clone = getattr(llm, "clone", None)
    if callable(clone):
        try:
            client = clone()
            client.model = model
            return client, "explicit"
        except Exception:  # noqa: BLE001
            pass
    return llm, "follow"


def _prompt(episode, existing=()):
    payload = {
        "goal": str(episode.get("goal") or "")[:300],
        "actions": [str(item.get("tool") or "") for item in episode.get("actions") or []][:20],
        "result": str((episode.get("result") or {}).get("summary") or "")[:MAX_SUMMARY],
        "files": episode.get("files") or [],
        "errors": episode.get("errors") or [],
        "next": str(episode.get("next") or "")[:200],
        "receipts": episode.get("receipts") or [],
        "existing": [{
            "id": str(item.get("id") or ""),
            "kind": str(item.get("kind") or ""),
            "text": str(item.get("body") or "")[:240],
            "updated": str(item.get("updated") or ""),
        } for item in existing],
    }
    return (
        "你在维护跨会话的项目记忆。下面是一轮已完成任务的摘要，以及已有的项目候选。"
        "判断其中是否有可复用的确定事实或经验教训；寒暄、命令回显、一次性细节、"
        "未验证的猜测都不要记。\n"
        "如果本轮结果证明某条已有候选已经过时或错误（例如旧诊断已被修复），"
        "把它的 id 放进 supersedes；只是主题相关、并不矛盾的不要列。\n"
        "只输出一个 JSON 对象，不要解释、不要代码围栏：\n"
        '{"keep": true 或 false, "kind": "fact" 或 "lesson", '
        '"title": "短标题", "text": "脱离本轮上下文也能读懂的独立结论，最多 300 字", '
        '"importance": 0.0 到 1.0, "supersedes": ["旧候选 id"]}\n'
        "规则：不得包含绝对路径、密钥、项目名；不确定就 keep=false。\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def run_once(project_id, session_id, llm, episode=None):
    """同步执行一次固化（测试与 worker 共用）。"""
    start = time.perf_counter()
    if not enabled():
        return {"ok": False, "reason": "disabled"}
    ep = episode or (episodes.latest(project_id, session_id, 1) or [None])[0]
    if not isinstance(ep, dict):
        return {"ok": False, "reason": "no_episode"}
    reason = _trivial(ep)
    if reason:
        metrics.record("consolidate.skip", project=project_id, session=session_id,
                       episode=ep.get("id"), reason=reason)
        return {"ok": False, "reason": reason}
    if llm is None:
        return {"ok": False, "reason": "no_llm"}
    client, mode = _client(llm)
    existing = sorted(
        [item for item in promote.candidates(project_id)
         if item.get("status") == "candidate"],
        key=lambda item: str(item.get("updated") or ""), reverse=True)[:10]
    try:
        output = client.chat([{"role": "user", "content": _prompt(ep, existing)}],
                             stream=False, temperature=0.2, max_output_tokens=1024)
    except Exception as exc:  # noqa: BLE001
        metrics.record("consolidate.error", project=project_id, session=session_id,
                       episode=ep.get("id"), error=str(exc)[:200],
                       latency_ms=round((time.perf_counter() - start) * 1000))
        return {"ok": False, "reason": "llm_error"}
    usage_in, usage_out = _tokens(getattr(client, "last_usage", None))
    data = _extract_json(output)
    if not isinstance(data, dict):
        metrics.record("consolidate.error", project=project_id, session=session_id,
                       episode=ep.get("id"), error="bad_json", model=getattr(client, "model", ""),
                       tokens_in=usage_in, tokens_out=usage_out,
                       latency_ms=round((time.perf_counter() - start) * 1000))
        return {"ok": False, "reason": "bad_json"}
    if not data.get("keep", False):
        metrics.record("consolidate.skip", project=project_id, session=session_id,
                       episode=ep.get("id"), reason="keep_false",
                       model=getattr(client, "model", ""), mode=mode,
                       tokens_in=usage_in, tokens_out=usage_out,
                       latency_ms=round((time.perf_counter() - start) * 1000))
        return {"ok": True, "kept": False}
    text = str(data.get("text") or data.get("summary") or "").strip()
    kind = "lesson" if str(data.get("kind") or "") == "lesson" else "fact"
    try:
        importance = min(1.0, max(0.0, float(data.get("importance"))))
    except (TypeError, ValueError):
        importance = 0.6
    candidate = promote.create_candidate(
        project_id, kind, text, importance=importance,
        source_turn=str(ep.get("id") or ""),
        receipt_ids=ep.get("receipts") or [], task=str(ep.get("task") or ""))
    new_id = str((candidate or {}).get("id") or "")
    known = {str(item.get("id") or "") for item in existing}
    requested = data.get("supersedes")
    if not isinstance(requested, (list, tuple)):
        requested = []
    superseded = []
    for old_id in requested[:3]:
        old_id = str(old_id or "")
        if (old_id and old_id != new_id and old_id in known
                and promote.supersede(project_id, old_id, new_id,
                                      reason="新回合证据取代旧候选")):
            superseded.append(old_id)
    swept = promote.sweep(project_id)
    metrics.record("consolidate", project=project_id, session=session_id,
                   episode=ep.get("id"), candidate=(candidate or {}).get("id"),
                   new=bool((candidate or {}).get("_new")), kind=kind,
                   model=getattr(client, "model", ""), mode=mode,
                   tokens_in=usage_in, tokens_out=usage_out,
                   latency_ms=round((time.perf_counter() - start) * 1000),
                   promoted=swept.get("promoted", 0),
                   superseded=len(superseded))
    return {"ok": True, "kept": True, "candidate": (candidate or {}).get("id"),
            "promoted": swept.get("promoted", 0), "superseded": superseded}


def schedule(project_id, session_id, llm, episode=None):
    """非阻塞提交；同一会话已有任务在跑时跳过这一轮，避免长任务堆队列。"""
    if not enabled() or llm is None:
        return False
    key = (str(project_id or ""), str(session_id or ""))
    with _LOCK:
        if key in _INFLIGHT:
            metrics.record("consolidate.skip", project=project_id, session=session_id,
                           reason="inflight")
            return False
        _INFLIGHT.add(key)

    def _job():
        try:
            run_once(project_id, session_id, llm, episode)
        except Exception:  # noqa: BLE001 —— worker 绝不把异常抛回主流程
            pass
        finally:
            with _LOCK:
                _INFLIGHT.discard(key)

    try:
        _EXECUTOR.submit(_job)
        return True
    except Exception:  # noqa: BLE001
        with _LOCK:
            _INFLIGHT.discard(key)
        return False
