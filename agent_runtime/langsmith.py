"""Optional LangSmith exporter for the local trace ledger.

The core runtime never imports the LangSmith SDK eagerly and never sends
prompt/answer/tool arguments.  Export is enabled explicitly with
``DOCMIND_LANGSMITH=1`` (or the standard ``LANGCHAIN_TRACING_V2=true`` plus
``LANGCHAIN_API_KEY``), so an offline installation behaves exactly as before.
"""
from __future__ import annotations

import atexit
import os
import threading
import time
import uuid
import contextvars
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping


_ASYNC_LOCK = threading.RLock()
_ASYNC_PENDING: set[Any] = set()
_ASYNC_ERRORS: list[dict[str, Any]] = []


def _retry_attempts() -> int:
    try:
        return max(1, min(8, int(os.getenv("DOCMIND_LANGSMITH_MAX_RETRIES", "3")) + 1))
    except (TypeError, ValueError):
        return 4


def _retry_base_seconds() -> float:
    try:
        return max(0.0, min(10.0, float(os.getenv("DOCMIND_LANGSMITH_RETRY_BASE_S", "0.25"))))
    except (TypeError, ValueError):
        return 0.25


def _record_async_error(name: str, exc: BaseException) -> None:
    with _ASYNC_LOCK:
        _ASYNC_ERRORS.append({"name": str(name)[:80], "error": type(exc).__name__})
        del _ASYNC_ERRORS[:-20]


def _run_with_retry(fn: Any, name: str) -> None:
    attempts = _retry_attempts()
    delay = _retry_base_seconds()
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            fn()
            return
        except BaseException as exc:  # observability must never affect the workflow
            last = exc
            if attempt + 1 >= attempts:
                break
            if delay:
                time.sleep(delay * (2 ** attempt))
    if last is not None:
        _record_async_error(name, last)


def _submit_async(fn: Any, name: str) -> None:
    """Run an exporter with bounded retries and retain it for shutdown flush."""
    holder: dict[str, Any] = {}

    def run() -> None:
        try:
            _run_with_retry(fn, name)
        finally:
            with _ASYNC_LOCK:
                _ASYNC_PENDING.discard(holder.get("thread"))

    thread = threading.Thread(target=run, name="docmind-langsmith-%s" % str(name)[:40],
                              daemon=False)
    holder["thread"] = thread
    with _ASYNC_LOCK:
        _ASYNC_PENDING.add(thread)
    try:
        thread.start()
    except BaseException as exc:
        with _ASYNC_LOCK:
            _ASYNC_PENDING.discard(thread)
        _record_async_error(name, exc)


def flush(timeout: float | None = None) -> dict[str, Any]:
    """Wait for outstanding exports, primarily for shutdown and tests."""
    if timeout is None:
        try:
            timeout = max(0.0, min(30.0, float(os.getenv("DOCMIND_LANGSMITH_FLUSH_TIMEOUT_S", "5"))))
        except (TypeError, ValueError):
            timeout = 5.0
    deadline = time.monotonic() + timeout
    while True:
        with _ASYNC_LOCK:
            pending = list(_ASYNC_PENDING)
        if not pending:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        for thread in pending:
            join = getattr(thread, "join", None)
            if callable(join):
                join(max(0.0, min(remaining, 0.25)))
    with _ASYNC_LOCK:
        remaining = len(_ASYNC_PENDING)
        errors = list(_ASYNC_ERRORS)
    return {"flushed": remaining == 0, "pending": remaining, "errors": errors}


atexit.register(flush)


def enabled() -> bool:
    explicit = os.getenv("DOCMIND_LANGSMITH", "").strip().lower()
    standard = os.getenv("LANGCHAIN_TRACING_V2", "").strip().lower()
    key = os.getenv("LANGCHAIN_API_KEY", "").strip()
    return bool(key and (explicit in {"1", "true", "yes", "on"}
                         or standard in {"1", "true", "yes", "on"}))


def status() -> dict[str, Any]:
    """Return safe diagnostics without exposing the API key."""
    try:
        from importlib.util import find_spec
        installed = find_spec("langsmith") is not None
    except Exception:
        installed = False
    key = bool(os.getenv("LANGCHAIN_API_KEY", "").strip())
    with _ASYNC_LOCK:
        pending = len(_ASYNC_PENDING)
        errors = list(_ASYNC_ERRORS[-5:])
    return {
        "installed": installed,
        "key_configured": key,
        "enabled": enabled(),
        "project": os.getenv("LANGCHAIN_PROJECT") or "default",
        "endpoint": os.getenv("LANGCHAIN_ENDPOINT") or "https://api.smith.langchain.com",
        "realtime": enabled() and installed,
        "pending_exports": pending,
        "recent_export_errors": errors,
    }


# The workflow ledger remains the source of truth.  These helpers only mirror
# bounded metadata to LangSmith and are deliberately tolerant of SDK version
# differences and network failures.
_EVENT_NAMES = {
    "clarify": "choice",
    "options_generated": "choice",
    "choice": "choice",
    "research": "research",
    "research_gate": "research",
    "approval_required": "approval",
    "approval_granted": "approval",
    "approval_denied": "approval",
    "execute_start": "execute",
    "execute_wave": "execute.wave",
    "wave": "execute.wave",
    "task": "subagent",
    "task_complete": "subagent",
    "before_tool": "tool.before",
    "after_tool": "tool.after",
    "before_mcp": "mcp.before",
    "after_mcp": "mcp.after",
    "mcp_retry": "mcp.retry",
    "before_subagent": "subagent.before",
    "after_subagent": "subagent.after",
    "graph_replan_start": "replan",
    "graph_replan_proposal": "replan",
    "replan": "replan",
    "review": "review",
    "review_failed": "review",
    "graph_loop": "execute.loop",
    "evaluation": "evaluation",
    "complete": "complete",
    "fail": "fail",
}

_CURRENT_PARENT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "docmind_langsmith_parent", default="")

_SAFE_EVENT_KEYS = {
    "status", "phase", "source", "option_source", "plan_source", "task_source",
    "option_count", "task_count", "count", "n_tasks", "n_ok", "n_failed", "steps",
    "steps_used", "replans", "replan_count", "attempt", "ok", "approved", "reason",
    "error", "backend", "task_id", "role", "task_thread", "thread_id", "duration_ms",
    "id",
    "score", "pass_rate", "failed_tasks", "blocked_tasks", "idempotency_in_doubt",
    "case_id",
    "tool", "argument_chars", "result_chars", "connector", "attempt",
    "reflection_ok", "mcp", "persona", "depth",
}


def _client() -> Any | None:
    if not enabled():
        return None
    try:
        from langsmith import Client
        return Client(api_url=os.getenv("LANGCHAIN_ENDPOINT") or None,
                      api_key=os.getenv("LANGCHAIN_API_KEY") or None)
    except Exception:
        return None


def _run_id(value: Any = None) -> str:
    return str(value or uuid.uuid4())


def _safe_scalar(key: str, value: Any) -> Any:
    """Keep trace payloads metadata-only; never forward free-form content."""
    if key in {"error", "reason"}:
        return type(value).__name__ if value else ""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        # IDs and bounded categories are useful; arbitrary text is not.
        return value[:80] if key in {"tool", "connector", "mcp", "persona"} or key == "id" or key.endswith(("_id", "_source", "backend", "role", "status", "phase", "reason")) else type(value).__name__
    return type(value).__name__


def _safe_event(kind: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
    safe: dict[str, Any] = {"kind": str(kind)[:64]}
    for key, value in (payload or {}).items():
        key = str(key)
        if key not in _SAFE_EVENT_KEYS:
            continue
        safe[key] = _safe_scalar(key, value)
    return safe


def _create_run(client: Any, **kwargs: Any) -> None:
    """Call create_run across old/new LangSmith SDK keyword conventions."""
    try:
        client.create_run(**kwargs)
    except TypeError:
        run_id = kwargs.pop("id", None)
        if run_id:
            kwargs["run_id"] = run_id
        client.create_run(**kwargs)


def _update_run(client: Any, run_id: str, **kwargs: Any) -> None:
    try:
        client.update_run(run_id, **kwargs)
    except TypeError:
        kwargs["run_id"] = run_id
        client.update_run(**kwargs)


@contextmanager
def child_span(parent_id: str, name: str, *, run_type: str = "chain",
               inputs: Mapping[str, Any] | None = None) -> Iterator[str | None]:
    """Create a child run from an explicit parent, including worker threads."""
    parent_id = str(parent_id or "")
    span_id = _run_id() if enabled() and parent_id else None
    started = int(time.time() * 1000)
    client = _client() if span_id else None
    if client is not None and span_id:
        try:
            _create_run(client, name="docmind.game.%s" % str(name)[:80],
                        run_type=run_type, id=span_id, parent_run_id=parent_id,
                        inputs=dict(inputs or {}), start_time=started,
                        project_name=os.getenv("LANGCHAIN_PROJECT") or "default",
                        tags=["docmind", "game-workflow", "realtime"])
        except Exception:
            pass
    token = _CURRENT_PARENT.set(span_id or parent_id)
    try:
        yield span_id
    except BaseException as exc:
        if client is not None and span_id:
            try:
                _update_run(client, span_id, error=type(exc).__name__,
                            end_time=int(time.time() * 1000))
            except Exception:
                pass
        raise
    else:
        if client is not None and span_id:
            try:
                _update_run(client, span_id, outputs={"ok": True},
                            end_time=int(time.time() * 1000))
            except Exception:
                pass
    finally:
        _CURRENT_PARENT.reset(token)


def start_workflow(state: Mapping[str, Any]) -> dict[str, Any]:
    """Start a root run and return only durable identifiers/flags."""
    if not enabled():
        return {}
    root_id = _run_id()
    now = int(time.time() * 1000)
    payload = {
        "workflow_id": str(state.get("workflow_id") or "")[:120],
        "request_chars": len(str(state.get("request") or "")),
        "sources": [str(item)[:40] for item in list(state.get("sources") or [])[:8]],
    }

    def send() -> None:
        client = _client()
        if client is None:
            return
        try:
            _create_run(client, name="docmind.game.workflow", run_type="chain", id=root_id,
                        inputs=payload, start_time=now,
                        project_name=os.getenv("LANGCHAIN_PROJECT") or "default",
                        tags=["docmind", "game-workflow", "realtime"],
                        extra={"metadata": {"workflow_id": payload["workflow_id"]}})
        except Exception:
            raise

    _submit_async(send, "root")
    return {"root_run_id": root_id, "event_count": 0, "realtime": True}


def record_workflow_event(state: Mapping[str, Any], kind: str,
                          payload: Mapping[str, Any] | None = None) -> None:
    """Mirror one workflow event as a completed child run and update root."""
    trace = state.get("langsmith_trace") or {}
    root_id = str(trace.get("root_run_id") or "")
    if not enabled() or not root_id:
        return
    safe = _safe_event(kind, payload)
    # The actual parallel Subagent span is created inside the worker with an
    # explicit parent.  Its completion event updates the root only, avoiding a
    # duplicate remote child run.
    child_id = "" if str(kind) == "task_complete" else _run_id()
    now = int(time.time() * 1000)
    name = _EVENT_NAMES.get(str(kind), "event")
    workflow_id = str(state.get("workflow_id") or "")[:120]

    def send() -> None:
        client = _client()
        if client is None:
            return
        try:
            if child_id:
                _create_run(client, name="docmind.game.%s" % name,
                            run_type="llm" if name in {"subagent", "execute.wave"} else "chain",
                            id=child_id, parent_run_id=root_id,
                            inputs={"workflow_id": workflow_id, "event": str(kind)[:64]},
                            outputs=safe, start_time=now, end_time=int(time.time() * 1000),
                            project_name=os.getenv("LANGCHAIN_PROJECT") or "default",
                            tags=["docmind", "game-workflow", name, "realtime"])
            _update_run(client, root_id,
                        outputs={"status": str(state.get("status") or "")[:40],
                                 "phase": str(state.get("phase") or "")[:40],
                                 "last_event": str(kind)[:64],
                                 "event_count": int(trace.get("event_count", 0)) + 1},
                        extra={"metadata": {"last_child_run_id": child_id or None}})
        except Exception:
            raise

    _submit_async(send, "event")


def finish_workflow(state: Mapping[str, Any]) -> None:
    """Close a persisted root run without uploading workflow content."""
    trace = state.get("langsmith_trace") or {}
    root_id = str(trace.get("root_run_id") or "")
    if not enabled() or not root_id:
        return
    outputs = {
        "status": str(state.get("status") or "")[:40],
        "phase": str(state.get("phase") or "")[:40],
        "steps": int(state.get("steps") or 0),
        "replans": int(state.get("replans") or 0),
        "review_ok": bool((state.get("review") or {}).get("ok")),
        "event_count": len(list(state.get("events") or [])),
    }

    def send() -> None:
        client = _client()
        if client is None:
            return
        try:
            _update_run(client, root_id, outputs=outputs,
                        end_time=int(time.time() * 1000),
                        extra={"metadata": {"terminal": True}})
        except Exception:
            raise

    _submit_async(send, "finish")


def _run_outputs(run: Any) -> Mapping[str, Any]:
    if isinstance(run, Mapping):
        value = run.get("outputs") or run
    else:
        value = getattr(run, "outputs", None) or {}
    return value if isinstance(value, Mapping) else {}


def workflow_evaluator(run: Any, example: Any = None) -> dict[str, Any]:
    """LangSmith-compatible evaluator backed by the local deterministic gate."""
    from .workflow_eval import evaluate_workflow
    outputs = _run_outputs(run)
    report = evaluate_workflow(outputs)
    return {
        "key": "workflow_quality",
        "score": float(report.get("score") or 0.0),
        "value": "pass" if report.get("passed") else "fail",
        "comment": "checks=%s" % ",".join(
            str(item.get("name")) for item in report.get("checks", [])
            if isinstance(item, Mapping) and item.get("ok")),
    }


def sync_workflow_dataset(dataset_name: str | None = None,
                          cases: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Create/update the fixed synthetic dataset when LangSmith is enabled."""
    from .workflow_eval import DEFAULT_DATASET_NAME, dataset_cases
    name = (dataset_name or os.getenv("DOCMIND_LANGSMITH_DATASET") or
            DEFAULT_DATASET_NAME).strip()[:120]
    rows = list(cases or dataset_cases())
    if not enabled():
        return {"enabled": False, "dataset": name, "examples": 0, "reason": "disabled"}
    client = _client()
    if client is None:
        return {"enabled": False, "dataset": name, "examples": 0, "reason": "client_unavailable"}
    try:
        try:
            dataset = client.read_dataset(dataset_name=name)
        except Exception:
            dataset = client.create_dataset(
                dataset_name=name,
                description="Synthetic game workflow regression cases for Agent Workbench Harness",
                metadata={"docmind": "game-workflow", "version": "1"},
            )
        dataset_id = getattr(dataset, "id", None) or (dataset.get("id") if isinstance(dataset, Mapping) else None)
        created = 0
        for case in rows:
            case = dict(case)
            case_id = str(case.get("id") or "case")[:80]
            example_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "docmind:%s:%s" % (name, case_id)))
            example = {
                "inputs": dict(case.get("inputs") or {}),
                "outputs": dict(case.get("outputs") or {}),
                "dataset_id": dataset_id,
                "metadata": {"docmind_case_id": case_id},
                "example_id": example_id,
            }
            try:
                client.create_example(**example)
            except Exception:
                updater = getattr(client, "update_example", None)
                if not callable(updater):
                    raise
                updater(example_id, inputs=example["inputs"], outputs=example["outputs"],
                        dataset_id=dataset_id, metadata=example["metadata"])
            created += 1
        return {"enabled": True, "dataset": name, "dataset_id": str(dataset_id or ""),
                "examples": created}
    except Exception as exc:
        return {"enabled": False, "dataset": name, "examples": 0,
                "reason": type(exc).__name__}


def evaluate_workflow_dataset(target: Any, dataset_name: str | None = None,
                              *, blocking: bool = True) -> dict[str, Any]:
    """Run the local evaluator through LangSmith's dataset evaluation API."""
    name = (dataset_name or os.getenv("DOCMIND_LANGSMITH_DATASET") or
            "docmind-game-workflows").strip()[:120]
    if not enabled():
        return {"enabled": False, "dataset": name, "reason": "disabled"}
    client = _client()
    if client is None:
        return {"enabled": False, "dataset": name, "reason": "client_unavailable"}
    try:
        result = client.evaluate(
            target, data=name, evaluators=[workflow_evaluator],
            blocking=bool(blocking), upload_results=True,
            experiment_prefix="docmind-game",
            error_handling="log",
        )
        return {"enabled": True, "dataset": name, "result": result}
    except Exception as exc:
        return {"enabled": False, "dataset": name, "reason": type(exc).__name__}


@contextmanager
def llm_call(*, name: str = "llm.call", session_id: str = "",
             message_count: int = 0, input_chars: int = 0) -> Iterator[str | None]:
    """Trace one model call under the current workflow/span parent if present."""
    parent_id = _CURRENT_PARENT.get("")
    span_id = _run_id() if enabled() and parent_id else None
    started = int(time.time() * 1000)
    client = _client() if span_id else None
    if client is not None and span_id:
        try:
            _create_run(client, name="docmind.game.%s" % str(name)[:64], run_type="llm",
                        id=span_id, parent_run_id=parent_id,
                        inputs={"session_id": str(session_id)[:120],
                                "message_count": int(message_count),
                                "input_chars": int(input_chars)},
                        start_time=started,
                        project_name=os.getenv("LANGCHAIN_PROJECT") or "default",
                        tags=["docmind", "llm", "realtime"])
        except Exception:
            pass
    try:
        yield span_id
    except BaseException as exc:
        if client is not None and span_id:
            try:
                _update_run(client, span_id, error=type(exc).__name__,
                            end_time=int(time.time() * 1000))
            except Exception:
                pass
        raise
    else:
        if client is not None and span_id:
            try:
                _update_run(client, span_id, outputs={"ok": True},
                            end_time=int(time.time() * 1000))
            except Exception:
                pass


@contextmanager
def tool_call(*, name: str = "tool.call", session_id: str = "",
              input_chars: int = 0) -> Iterator[str | None]:
    """Trace one tool/MCP call under the current workflow/span parent."""
    parent_id = _CURRENT_PARENT.get("")
    span_id = _run_id() if enabled() and parent_id else None
    started = int(time.time() * 1000)
    client = _client() if span_id else None
    if client is not None and span_id:
        try:
            _create_run(client, name="docmind.game.%s" % str(name)[:64], run_type="tool",
                        id=span_id, parent_run_id=parent_id,
                        inputs={"session_id": str(session_id)[:120],
                                "input_chars": int(input_chars)},
                        start_time=started,
                        project_name=os.getenv("LANGCHAIN_PROJECT") or "default",
                        tags=["docmind", "tool", "realtime"])
        except Exception:
            pass
    try:
        yield span_id
    except BaseException as exc:
        if client is not None and span_id:
            try:
                _update_run(client, span_id, error=type(exc).__name__,
                            end_time=int(time.time() * 1000))
            except Exception:
                pass
        raise
    else:
        if client is not None and span_id:
            try:
                _update_run(client, span_id, outputs={"ok": True},
                            end_time=int(time.time() * 1000))
            except Exception:
                pass


@contextmanager
def workflow_span(state: Mapping[str, Any], name: str, *, run_type: str = "chain",
                  inputs: Mapping[str, Any] | None = None) -> Iterator[str | None]:
    """Create a bounded child span for adapters such as LLM/tool calls."""
    trace = state.get("langsmith_trace") or {}
    parent_id = str(trace.get("root_run_id") or "") or _CURRENT_PARENT.get("")
    span_id = _run_id() if enabled() and parent_id else None
    started = int(time.time() * 1000)
    if span_id:
        client = _client()
        if client is not None:
            try:
                _create_run(client, name="docmind.game.%s" % str(name)[:64], run_type=run_type,
                            id=span_id, parent_run_id=parent_id,
                            inputs={"workflow_id": str(state.get("workflow_id") or "")[:120],
                                    **dict(inputs or {})}, start_time=started,
                            project_name=os.getenv("LANGCHAIN_PROJECT") or "default",
                            tags=["docmind", "game-workflow", "realtime"])
            except Exception:
                pass
    token = _CURRENT_PARENT.set(span_id or parent_id)
    try:
        yield span_id
    except BaseException as exc:
        if span_id:
            client = _client()
            if client is not None:
                try:
                    _update_run(client, span_id, error=type(exc).__name__,
                                end_time=int(time.time() * 1000))
                except Exception:
                    pass
        raise
    else:
        if span_id:
            client = _client()
            if client is not None:
                try:
                    _update_run(client, span_id, outputs={"ok": True},
                                end_time=int(time.time() * 1000))
                except Exception:
                    pass
    finally:
        _CURRENT_PARENT.reset(token)


@contextmanager
def retrieval_span(*, name: str = "retrieval.query", collection: str = "",
                   mode: str = "", query_chars: int = 0,
                   inputs: Mapping[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    """Trace a retrieval stage under the active workflow parent.

    Only bounded, non-content metadata is sent to LangSmith.  The yielded
    dictionary is intentionally local: callers may populate counts, scores,
    latency and fallback information, while source text and query text stay in
    the local retrieval event ledger used by the workbench.
    """
    parent_id = _CURRENT_PARENT.get("")
    span_id = _run_id() if enabled() and parent_id else None
    started = int(time.time() * 1000)
    client = _client() if span_id else None
    outputs: dict[str, Any] = {"ok": True}
    safe_inputs = {"collection": str(collection)[:120], "mode": str(mode)[:40],
                   "query_chars": max(0, int(query_chars))}
    for key, value in (inputs or {}).items():
        if key in {"top_k", "candidate_k", "reranker", "dense_weight",
                   "lexical_weight", "rrf_k"} and isinstance(value, (str, int, float, bool)):
            safe_inputs[key] = value
    if client is not None and span_id:
        try:
            _create_run(client, name="docmind.game.%s" % str(name)[:80],
                        run_type="retriever", id=span_id, parent_run_id=parent_id,
                        inputs=safe_inputs, start_time=started,
                        project_name=os.getenv("LANGCHAIN_PROJECT") or "default",
                        tags=["docmind", "retrieval", str(mode)[:40], "realtime"])
        except Exception:
            pass
    token = _CURRENT_PARENT.set(span_id or parent_id)
    try:
        yield outputs
    except BaseException as exc:
        outputs = {"ok": False, "error": type(exc).__name__}
        if client is not None and span_id:
            try:
                _update_run(client, span_id, error=type(exc).__name__,
                            end_time=int(time.time() * 1000))
            except Exception:
                pass
        raise
    else:
        if client is not None and span_id:
            try:
                safe_outputs = {key: value for key, value in outputs.items()
                                if key in {"ok", "documents", "candidates", "returned",
                                           "duration_ms", "reranker", "rerank_applied",
                                           "cache", "added", "removed", "updated",
                                           "fallback"} and
                                isinstance(value, (str, int, float, bool))}
                _update_run(client, span_id, outputs=safe_outputs,
                            end_time=int(time.time() * 1000))
            except Exception:
                pass
    finally:
        _CURRENT_PARENT.reset(token)


def _export(record: Mapping[str, Any]) -> None:
    if not enabled():
        return
    try:
        from langsmith import Client  # optional dependency, loaded on demand

        client = Client(
            api_url=os.getenv("LANGCHAIN_ENDPOINT") or None,
            api_key=os.getenv("LANGCHAIN_API_KEY") or None,
        )
        started = time.time() - (float(record.get("elapsed_ms") or 0) / 1000.0)
        inputs = {
            "turn_id": record.get("turn_id"),
            "session_id": record.get("session_id"),
            "question_chars": record.get("question_chars", 0),
            "messages_hash": record.get("messages_hash"),
            "messages_count": record.get("messages_count", 0),
        }
        outputs = {
            "outcome": record.get("outcome"),
            "finish_reason": record.get("finish_reason"),
            "verified": bool(record.get("verified")),
            "final_chars": record.get("final_chars", 0),
            "actions": list(record.get("actions") or []),
            "n_steps": record.get("n_steps", 0),
            "error": record.get("error"),
        }
        client.create_run(
            name="docmind.agent.turn",
            run_type="chain",
            inputs=inputs,
            outputs=outputs,
            start_time=int(started * 1000),
            end_time=int(time.time() * 1000),
            project_name=os.getenv("LANGCHAIN_PROJECT") or "default",
            tags=["docmind", str(record.get("provider") or "unknown")],
            extra={
                "metadata": {
                    "turn_id": record.get("turn_id"),
                    "route": record.get("route"),
                    "model": record.get("model"),
                    "prompt_tokens": record.get("prompt_tokens", 0),
                    "completion_tokens": record.get("completion_tokens", 0),
                    "cost_cny": record.get("cost_cny", 0),
                }
            },
        )
    except Exception:
        # The async dispatcher records the error and applies bounded retries.
        raise


def export_turn(record: Mapping[str, Any]) -> None:
    """Best-effort asynchronous export of one redacted turn record."""
    if not enabled():
        return
    _submit_async(lambda: _export(dict(record)), "turn")


def export_workflow(state: Mapping[str, Any]) -> None:
    """Export a redacted workflow tree (workflow root + lifecycle child runs).

    The local workflow event ledger is intentionally the source of truth.  We
    create the parent/child relationship only at export time so tracing never
    becomes a runtime dependency and no user prompt, tool argument, or tool
    output is sent to LangSmith.
    """
    if not enabled():
        return

    def send():
        try:
            from langsmith import Client
            client = Client(api_url=os.getenv("LANGCHAIN_ENDPOINT") or None,
                            api_key=os.getenv("LANGCHAIN_API_KEY") or None)
            events = list(state.get("events") or [])
            tasks = list(state.get("tasks") or [])
            event_summary = []
            for event in events[-100:]:
                if not isinstance(event, Mapping):
                    continue
                safe = {"kind": event.get("kind"), "ts": event.get("ts")}
                for key, value in event.items():
                    if key in {"kind", "ts"}:
                        continue
                    if isinstance(value, (str, int, float, bool)):
                        safe[key] = value if not isinstance(value, str) else value[:160]
                    elif isinstance(value, (list, tuple)):
                        safe[key] = [str(item)[:80] for item in value[:8]]
                event_summary.append(safe)
            root_id = str(uuid.uuid4())
            client.create_run(
                name="docmind.game.workflow",
                run_type="chain",
                id=root_id,
                inputs={"workflow_id": state.get("workflow_id"),
                        "request_chars": len(str(state.get("request") or "")),
                        "sources": list(state.get("sources") or [])},
                outputs={"status": state.get("status"),
                         "phase": state.get("phase"),
                         "task_ids": [t.get("id") for t in tasks if isinstance(t, Mapping)],
                         "event_kinds": [e.get("kind") for e in events if isinstance(e, Mapping)],
                         "event_summary": event_summary,
                         "review": state.get("review") or {},
                         "replans": state.get("replans", 0),
                         "steps": state.get("steps", 0)},
                project_name=os.getenv("LANGCHAIN_PROJECT") or "default",
                tags=["docmind", "game-workflow", str(state.get("status") or "unknown")],
            )
            child_kinds = {
                "clarify": "clarify", "choice": "choice", "research": "research",
                "plan": "plan", "approval_required": "approval",
                "approval_granted": "approval", "approval_denied": "approval",
                "execute_start": "execute", "wave": "execute.wave",
                "task": "subagent", "graph_replan_start": "replan",
                "graph_replan_proposal": "replan", "replan": "replan",
                "review": "review", "graph_loop": "execute.loop",
            }
            for index, event in enumerate(events[-100:]):
                if not isinstance(event, Mapping):
                    continue
                kind = str(event.get("kind") or "event")
                name = child_kinds.get(kind)
                if not name:
                    continue
                safe = {"kind": kind, "index": index}
                for key, value in event.items():
                    if key in {"kind", "ts"}:
                        continue
                    if isinstance(value, (str, int, float, bool)):
                        safe[key] = value if not isinstance(value, str) else value[:160]
                    elif isinstance(value, (list, tuple)):
                        safe[key] = [str(item)[:80] for item in value[:8]]
                client.create_run(
                    name="docmind.game.%s" % name,
                    run_type="chain" if name not in {"subagent", "execute.wave"} else "llm",
                    id=str(uuid.uuid4()), parent_run_id=root_id,
                    inputs={"workflow_id": state.get("workflow_id"), "event": kind},
                    outputs=safe,
                    project_name=os.getenv("LANGCHAIN_PROJECT") or "default",
                    tags=["docmind", "game-workflow", name],
                )
        except Exception:
            raise

    _submit_async(send, "workflow")
