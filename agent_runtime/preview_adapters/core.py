"""Build a safe, domain neutral preview from workflow result evidence.

Adapters are intentionally data based. A tool can return ``artifacts`` with a
kind, path/URI, before/after values, or evidence. Unknown artifacts remain
visible with a generic renderer instead of being discarded. Domain plugins can
add their own metadata without changing the workflow engine.
"""
from __future__ import annotations

import mimetypes
import json
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any, Iterable, Mapping

PREVIEW_SCHEMA = "docmind.preview.v1"
_SECRET = re.compile(r"(?i)(api[_-]?key|token|password|passwd|secret|private[_-]?key)\s*[:=]\s*\S+")
_EXTENSIONS = {
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "webp": "image", "svg": "image",
    "mp3": "audio", "wav": "audio", "ogg": "audio", "flac": "audio",
    "mp4": "video", "webm": "video", "mov": "video", "mkv": "video",
    "json": "structured", "yaml": "structured", "yml": "structured", "toml": "structured", "csv": "structured",
    "md": "text", "txt": "text", "log": "text", "patch": "diff", "diff": "diff",
    "py": "code", "js": "code", "ts": "code", "vue": "code", "tsx": "code", "jsx": "code",
    "gd": "code", "cs": "code", "cpp": "code", "c": "code", "h": "code", "java": "code", "rs": "code",
    "kicad_sch": "structured", "kicad_pcb": "structured", "blend": "binary", "glb": "model", "gltf": "model",
}

# A domain adapter only enriches evidence.  The workflow engine remains
# domain neutral: adapters receive one raw artifact and may return one or more
# protocol artifacts.  Keeping the registry here lets EDA, game, CAD, data,
# and future integrations plug in without adding branches to the executor.
PreviewAdapter = Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any] | Sequence[Mapping[str, Any]] | None]
_ADAPTERS: dict[str, PreviewAdapter] = {}


def register_preview_adapter(name: str, handler: PreviewAdapter, *, replace: bool = False) -> PreviewAdapter:
    """Register a named artifact adapter and return the handler.

    Registration is explicit so applications can install adapters during
    startup.  Accidental replacement is rejected unless ``replace=True``.
    """
    key = str(name or "").strip().lower()
    if not key or not callable(handler):
        raise ValueError("preview adapter name and callable handler are required")
    if key in _ADAPTERS and not replace:
        raise ValueError("preview adapter already registered: %s" % key)
    _ADAPTERS[key] = handler
    return handler


def unregister_preview_adapter(name: str) -> bool:
    return _ADAPTERS.pop(str(name or "").strip().lower(), None) is not None


def preview_adapters() -> tuple[str, ...]:
    """Return registered adapter names for diagnostics and UI discovery."""
    return tuple(sorted(_ADAPTERS))


def _clean(value: Any, limit: int = 1600) -> str:
    text = str(value or "").replace("\x00", "").strip()
    text = _SECRET.sub(lambda match: match.group(1) + "=[REDACTED]", text)
    return text[:limit]


def infer_artifact_kind(value: Any, mime: str = "") -> str:
    explicit = str(value or "").strip().lower()
    if explicit in {"code", "text", "diff", "image", "audio", "video", "model", "structured", "binary", "interactive", "live", "unknown"}:
        if explicit == "live":
            return "interactive"
        return explicit
    guessed = (mime or "").lower()
    if guessed.startswith("image/"): return "image"
    if guessed.startswith("audio/"): return "audio"
    if guessed.startswith("video/"): return "video"
    if guessed.startswith("text/") or guessed in {"application/json", "application/xml"}: return "text"
    suffix = str(value or "").lower().rsplit(".", 1)[-1] if "." in str(value or "") else ""
    return _EXTENSIONS.get(suffix, "unknown")


def _safe_uri(value: Any) -> str:
    text = _clean(value, 800)
    if text.startswith(("javascript:", "data:", "file:")):
        return ""
    return text


def _adapter_inputs(raw: Mapping[str, Any], workflow: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    # Workflow kind is the safe fallback for legacy tool results that only
    # contain a file path or screenshot and predate the adapter field.
    name = str(raw.get("adapter") or raw.get("domain") or workflow.get("kind") or "").strip().lower()
    handler = _ADAPTERS.get(name) if name else None
    if handler is None and name:
        # Agent-generated adapters are declarative project manifests. They can
        # enrich evidence without importing or executing arbitrary project code.
        root = str(workflow.get("project_root") or "").strip()
        manifest = Path(root).resolve() / ".docmind" / "preview-adapters" / (re.sub(r"[^a-z0-9_.-]+", "-", name) + ".json") if root else None
        try:
            spec = json.loads(manifest.read_text(encoding="utf-8")) if manifest and manifest.is_file() else {}
        except (OSError, ValueError, TypeError):
            spec = {}
        if isinstance(spec, Mapping) and spec.get("status") == "active":
            enriched = dict(raw)
            metadata = dict(enriched.get("metadata") or {})
            metadata.setdefault("generated_adapter", "true")
            metadata.setdefault("refresh_tool", str(spec.get("refresh_tool") or ""))
            metadata.setdefault("connector_hint", str(spec.get("connector_hint") or ""))
            enriched["metadata"] = metadata
            enriched.setdefault("summary", spec.get("evidence") or "模型生成的领域预览适配器")
            yield enriched
            return
    if handler is None:
        yield raw
        return
    try:
        adapted = handler(dict(raw), {"workflow": workflow, "schema": PREVIEW_SCHEMA})
    except Exception as exc:  # an optional adapter must not break the preview
        fallback = dict(raw)
        evidence = list(fallback.get("evidence") or [])
        fallback["evidence"] = evidence + ["适配器失败：%s" % type(exc).__name__]
        yield fallback
        return
    if adapted is None:
        return
    if isinstance(adapted, Mapping):
        yield dict(raw, **dict(adapted))
        return
    for item in adapted:
        if isinstance(item, Mapping):
            yield dict(raw, **dict(item))


def normalize_artifact(raw: Mapping[str, Any], *, fallback_id: str = "artifact") -> dict[str, Any]:
    path = _safe_uri(raw.get("path") or raw.get("uri") or raw.get("url") or raw.get("name"))
    mime = _clean(raw.get("mime") or raw.get("mime_type") or mimetypes.guess_type(path)[0] or "", 120)
    kind = infer_artifact_kind(raw.get("kind") or raw.get("artifact_kind") or path, mime)
    artifact_id = _clean(raw.get("id") or fallback_id, 100) or fallback_id
    result = {
        "id": artifact_id, "kind": kind, "label": _clean(raw.get("label") or raw.get("name") or path or artifact_id, 240),
        "renderer": _clean(raw.get("renderer") or {"image": "media", "audio": "media", "video": "media", "interactive": "interactive", "diff": "diff", "structured": "table", "code": "code", "text": "text"}.get(kind, "generic"), 40),
        "adapter": _clean(raw.get("adapter") or raw.get("domain") or "generic", 80),
        "path": path, "uri": _safe_uri(raw.get("uri") or raw.get("url")), "mime": mime,
        "status": _clean(raw.get("status") or "available", 40),
        "summary": _clean(raw.get("summary") or raw.get("description") or "", 1000),
        "before": _clean(raw.get("before") or "", 2400), "after": _clean(raw.get("after") or raw.get("content") or "", 2400),
        "evidence": [_clean(item, 400) for item in list(raw.get("evidence") or [])[:16]],
        "metadata": {str(k): _clean(v, 300) for k, v in dict(raw.get("metadata") or {}).items() if str(k).lower() not in {"token", "secret", "password", "api_key"}},
    }
    if result["before"] or result["after"]:
        before_lines = result["before"].count("\n") + (1 if result["before"] else 0)
        after_lines = result["after"].count("\n") + (1 if result["after"] else 0)
        before_set = set(result["before"].splitlines())
        after_set = set(result["after"].splitlines())
        result["change_summary"] = {
            "before_lines": before_lines,
            "after_lines": after_lines,
            "added_lines": max(0, len(after_set - before_set)),
            "removed_lines": max(0, len(before_set - after_set)),
        }
    return result


def _iter_result_artifacts(workflow: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    report = workflow.get("results") or {}
    result_map = report.get("results") if isinstance(report, Mapping) else None
    result_map = result_map if isinstance(result_map, Mapping) else {}
    for task_id, result in result_map.items():
        if not isinstance(result, Mapping):
            continue
        # Existing tools historically returned a preview URL at the result
        # level.  Promote those fields into the protocol automatically so
        # older engine/export tools gain the interactive renderer for free.
        live_url = result.get("preview_url") or result.get("live_url") or result.get("iframe_url")
        if live_url:
            yield str(task_id), {
                "id": "%s-live-preview" % task_id, "kind": "interactive",
                "uri": live_url, "label": result.get("preview_label") or "实时运行画面",
                "summary": result.get("preview_summary") or "工具返回的可交互预览",
                "evidence": ["task:" + str(task_id)],
            }
        for index, raw in enumerate(list(result.get("artifacts") or [])):
            if isinstance(raw, Mapping):
                yield str(task_id), dict(raw, evidence=list(raw.get("evidence") or []) + ["task:" + str(task_id)])
        # `file_changes` 与 `artifacts` 在真子代理下**必然重叠**：写工具自报的
        # 那一条 artifact 会被 `_run_child` 同时收进 artifacts 和 file_changes
        # （后者是给 `output_audit` 的证据字段，前者是给预览的产物字段）。
        # 实测同一个文件在最终 artifacts 里出现两次。
        #
        # 下面按 path 去重：`artifacts` 优先（它带 `bytes` 等更完整的元信息），
        # `file_changes` 里同 path 的那条只作为**兜底**——只有当 artifacts 里
        # 没有同 path 的条目时它才产出。这不是可有可无的整理，而是两条数据源
        # 指向同一事实时的必要去重。
        _already = {
            str(item.get("path") or "").strip()
            for item in list(result.get("artifacts") or [])
            if isinstance(item, Mapping)
        }
        for index, value in enumerate(list(result.get("file_changes") or [])):
            _path = str((value.get("path") if isinstance(value, Mapping) else value) or "").strip()
            if not _path or _path in _already:
                continue
            if isinstance(value, Mapping):
                yield str(task_id), dict(value, evidence=list(value.get("evidence") or []) + ["task:" + str(task_id)])
            else:
                yield str(task_id), {"path": value, "kind": infer_artifact_kind(value), "evidence": ["task:" + str(task_id)]}
        for index, value in enumerate(list(result.get("evidence") or [])):
            if isinstance(value, Mapping):
                yield str(task_id), dict(value, evidence=list(value.get("evidence") or []) + ["task:" + str(task_id)])


def build_preview_bundle(workflow: Mapping[str, Any]) -> dict[str, Any]:
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task_id, raw in _iter_result_artifacts(workflow):
        for adapted in _adapter_inputs(raw, workflow):
            item = normalize_artifact(adapted, fallback_id="%s-artifact-%s" % (task_id, len(artifacts) + 1))
            if item["id"] in seen: item["id"] += "-%s" % (len(artifacts) + 1)
            seen.add(item["id"])
            artifacts.append(item)
    review = workflow.get("review") if isinstance(workflow.get("review"), Mapping) else {}
    files: list[dict[str, Any]] = []
    file_seen: set[str] = set()
    for item in artifacts:
        path = item.get("path") or ""
        if not path or path in file_seen:
            continue
        file_seen.add(path)
        change = str(item.get("metadata", {}).get("change") or item.get("status") or "modified").lower()
        files.append({"path": path, "kind": change if change in {"added", "modified", "deleted"} else "modified",
                      "artifact_id": item["id"], "summary": item.get("change_summary") or {}})
    change_counts = {kind: sum(row["kind"] == kind for row in files) for kind in ("added", "modified", "deleted")}
    return {
        "schema": PREVIEW_SCHEMA, "workflow_id": _clean(workflow.get("workflow_id"), 100),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": _clean(workflow.get("status") or "unknown", 40),
        "summary": _clean(review.get("summary") or workflow.get("request") or "", 1600),
        "artifacts": artifacts,
        "changes": {"files": files, "counts": change_counts, "total": len(files)},
        "checks": [{"name": "workflow_review", "ok": bool(review.get("ok")), "detail": _clean(review.get("message") or "", 500)}],
        "counts": {"artifacts": len(artifacts), "by_kind": {kind: sum(item["kind"] == kind for item in artifacts) for kind in sorted({item["kind"] for item in artifacts})}},
    }


__all__ = ["PREVIEW_SCHEMA", "PreviewAdapter", "build_preview_bundle", "infer_artifact_kind",
           "normalize_artifact", "preview_adapters", "register_preview_adapter",
           "unregister_preview_adapter"]
