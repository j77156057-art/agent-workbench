"""Retrieval interfaces shared by the Harness and optional LangChain components.

The Harness owns embedding, Chroma storage, permissions and output shaping.  This
module only supplies the standard LangChain ``BaseRetriever`` boundary so a
different retriever or reranker can be plugged in later without changing the
Agent execution loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager, closing
from collections import deque
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from typing import Any, Iterable, Mapping, Sequence

from config import TOP_K, COLLECTION_NAME, STATE_ROOT
from embeddings import EmbeddingClient
from vectorstore import get_collection, query as vector_query

try:  # langchain-core is optional for source-only/offline installations
    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever
    from pydantic import PrivateAttr
except Exception:  # pragma: no cover - exercised only without langchain-core
    @dataclass(frozen=True)
    class Document:  # type: ignore[no-redef]
        page_content: str
        metadata: dict[str, Any]

    class BaseRetriever:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any):
            for key, value in kwargs.items():
                setattr(self, key, value)

        def invoke(self, query: str, **_: Any) -> list[Document]:
            return self._get_relevant_documents(query)

    PrivateAttr = None


@dataclass(frozen=True)
class RetrieverStatus:
    """Safe diagnostics for the selected retrieval boundary."""

    backend: str
    langchain_core: bool
    collection: str
    top_k: int


def _tokens(value: Any) -> list[str]:
    """Tokenize mixed English/code/Chinese text for deterministic BM25."""
    text = str(value or "").lower()
    out: list[str] = []
    for part in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            out.extend(part)
            out.extend(part[i:i + 2] for i in range(max(0, len(part) - 1)))
        else:
            out.append(part)
    return out


class BM25Index:
    """BM25 scoring core; snapshots are persisted by the selected lexical backend.

    The scorer itself stays deliberately pure/in-memory, while ``json`` and
    ``sqlite`` snapshot stores provide restart reuse, fingerprint checks and
    incremental added/removed/updated accounting around it.
    """

    def __init__(self, documents: Sequence[str], *, k1: float = 1.5, b: float = 0.75):
        self.documents = list(documents)
        self.k1 = float(k1)
        self.b = float(b)
        self.tokens = [_tokens(item) for item in self.documents]
        self.lengths = [len(item) for item in self.tokens]
        self.avgdl = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        self.doc_freq: dict[str, int] = {}
        for row in self.tokens:
            for term in set(row):
                self.doc_freq[term] = self.doc_freq.get(term, 0) + 1

    def scores(self, query: str) -> list[float]:
        if not self.tokens:
            return []
        q_terms = set(_tokens(query))
        n_docs = len(self.tokens)
        out: list[float] = []
        for row, length in zip(self.tokens, self.lengths):
            counts: dict[str, int] = {}
            for term in row:
                counts[term] = counts.get(term, 0) + 1
            score = 0.0
            for term in q_terms:
                freq = counts.get(term, 0)
                if not freq:
                    continue
                df = self.doc_freq.get(term, 0)
                idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
                norm = freq + self.k1 * (1.0 - self.b + self.b * length / (self.avgdl or 1.0))
                score += idf * (freq * (self.k1 + 1.0) / norm)
            out.append(score)
        return out


_LEXICAL_SCHEMA = 1
_LEXICAL_LOCK = threading.RLock()
_LEXICAL_MEMORY: dict[str, tuple[str, "BM25Index", dict[str, Any]]] = {}
_LEXICAL_SQLITE: dict[str, "SQLiteBM25Index"] = {}
_LEXICAL_STATS: dict[str, dict[str, Any]] = {}
_RETRIEVAL_EVENTS: deque[dict[str, Any]] = deque(maxlen=200)
_RETRIEVAL_EVENTS_LOCK = threading.RLock()


@contextmanager
def _retrieval_span(*, name: str, collection: str, mode: str,
                    query_chars: int, inputs: Mapping[str, Any] | None = None):
    """Best-effort LangSmith span without making retrieval depend on it."""
    try:
        from .langsmith import retrieval_span
    except Exception:
        yield {}
        return
    with retrieval_span(name=name, collection=collection, mode=mode,
                        query_chars=query_chars, inputs=inputs) as meta:
        yield meta


def _retrieval_doc_metadata(document: Document) -> dict[str, Any]:
    metadata = dict(document.metadata or {})
    row: dict[str, Any] = {
        "id": str(metadata.get("_id") or "")[:160],
        "source": str(metadata.get("source") or "")[:240],
        "snippet": str(document.page_content or "")[:360],
    }
    for key in ("start_line", "end_line", "dense_rank", "bm25_score",
                "hybrid_score", "rerank_score", "distance"):
        value = metadata.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            row[key] = round(float(value), 6) if isinstance(value, float) else value
        elif value is not None and key in {"start_line", "end_line"}:
            row[key] = str(value)[:40]
    return row


def _record_retrieval_event(*, query: str, collection: str, mode: str,
                            documents: Sequence[Document], duration_ms: float,
                            **extra: Any) -> None:
    """Keep a bounded local ledger for the workbench; never upload content."""
    event: dict[str, Any] = {
        "ts": time.time(),
        "query": str(query or "")[:240],
        "collection": str(collection or "")[:120],
        "mode": str(mode or "")[:40],
        "duration_ms": round(max(0.0, float(duration_ms)), 2),
        "documents": [_retrieval_doc_metadata(item) for item in list(documents)[:12]],
    }
    for key, value in extra.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            event[str(key)] = value
        elif isinstance(value, Mapping):
            event[str(key)] = {str(k): v for k, v in list(value.items())[:16]
                               if isinstance(v, (str, int, float, bool)) or v is None}
    with _RETRIEVAL_EVENTS_LOCK:
        _RETRIEVAL_EVENTS.append(event)


def retrieval_events(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent local retrieval diagnostics for the workbench."""
    count = max(1, min(200, int(limit)))
    with _RETRIEVAL_EVENTS_LOCK:
        rows = list(_RETRIEVAL_EVENTS)[-count:]
    return [dict(row, documents=[dict(item) for item in row.get("documents", [])])
            for row in reversed(rows)]


def _lexical_dir() -> str:
    return os.getenv(
        "DOCMIND_LEXICAL_INDEX_DIR",
        os.path.join(STATE_ROOT, ".docmind", "retrieval"),
    )


def _lexical_path(collection: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(collection or "collection"))[:120]
    return os.path.join(_lexical_dir(), safe + ".bm25.json")


def _lexical_sqlite_path(collection: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(collection or "collection"))[:120]
    return os.path.join(_lexical_dir(), safe + ".bm25.sqlite3")


def _snapshot_fingerprint(corpus: Sequence[Document]) -> str:
    rows = []
    for item in corpus:
        metadata = dict(item.metadata or {})
        rows.append({
            "id": str(metadata.get("_id") or ""),
            "source": str(metadata.get("source") or ""),
            "content": str(item.page_content or ""),
        })
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def _lexical_delta(previous: Sequence[Mapping[str, Any]], corpus: Sequence[Document]) -> dict[str, int]:
    old = {str(row.get("id") or (row.get("source", ""), row.get("content", ""))):
           str(row.get("content") or "") for row in (previous or [])}
    current = {}
    for item in corpus:
        metadata = dict(item.metadata or {})
        key = str(metadata.get("_id") or (metadata.get("source", ""), item.page_content))
        current[key] = str(item.page_content or "")
    added = set(current) - set(old)
    removed = set(old) - set(current)
    updated = {key for key in set(current) & set(old) if current[key] != old[key]}
    return {"added": len(added), "removed": len(removed), "updated": len(updated)}


def _read_lexical_file(path: str) -> tuple[str, list[dict[str, Any]]] | None:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict) or int(value.get("schema", 0)) != _LEXICAL_SCHEMA:
            return None
        fingerprint = str(value.get("fingerprint") or "")
        rows = value.get("documents") or []
        if not fingerprint or not isinstance(rows, list):
            return None
        return fingerprint, [dict(row) for row in rows if isinstance(row, Mapping)]
    except (OSError, ValueError, TypeError):
        return None


def _write_lexical_file(path: str, fingerprint: str, corpus: Sequence[Document]) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = []
        for item in corpus:
            metadata = dict(item.metadata or {})
            rows.append({
                "id": str(metadata.get("_id") or ""),
                "source": str(metadata.get("source") or ""),
                "content": str(item.page_content or ""),
            })
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as stream:
            json.dump({"schema": _LEXICAL_SCHEMA, "fingerprint": fingerprint,
                       "documents": rows}, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, path)
        return True
    except (OSError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class LexicalHit:
    """A lexical result returned without scanning the whole corpus."""

    key: str
    score: float
    document: Document


class SQLiteBM25Index:
    """Persistent BM25 postings index.

    The index is synchronized from Chroma only on first use or after an
    invalidation.  Normal queries read only postings for query terms, which
    avoids loading every document body into Python on every hybrid search.
    """

    schema = 1

    def __init__(self, collection: str, path: str):
        self.collection = str(collection)
        self.path = path
        self._ready = False
        self._fingerprint = ""
        self._documents = 0

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS lexical_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lexical_documents (
                doc_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT NOT NULL,
                length INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lexical_postings (
                term TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                term_frequency INTEGER NOT NULL,
                PRIMARY KEY (term, doc_id)
            );
            CREATE INDEX IF NOT EXISTS lexical_postings_term_idx
                ON lexical_postings(term);
            """
        )
        return connection

    @staticmethod
    def _row_key(item: Document) -> str:
        metadata = dict(item.metadata or {})
        return str(metadata.get("_id") or (metadata.get("source", ""), item.page_content))

    @staticmethod
    def _row_payload(item: Document) -> tuple[str, str, str, str, list[str]]:
        metadata = dict(item.metadata or {})
        key = SQLiteBM25Index._row_key(item)
        content = str(item.page_content or "")
        source = str(metadata.get("source") or "")
        safe_metadata = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        tokens = _tokens(content)
        return key, source, content, safe_metadata, tokens

    def _meta(self, connection: sqlite3.Connection, key: str, default: str = "") -> str:
        row = connection.execute("SELECT value FROM lexical_meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row is not None else default

    def _set_meta(self, connection: sqlite3.Connection, key: str, value: Any) -> None:
        connection.execute(
            "INSERT INTO lexical_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def ready(self) -> bool:
        if self._ready:
            return True
        try:
            with closing(self._connect()) as connection:
                self._fingerprint = self._meta(connection, "fingerprint")
                self._documents = int(self._meta(connection, "documents", "0") or 0)
                self._ready = bool(self._fingerprint)
        except (OSError, sqlite3.Error, ValueError):
            self._ready = False
        return self._ready

    def invalidate(self) -> None:
        self._ready = False

    def sync(self, corpus: Sequence[Document], fingerprint: str) -> dict[str, int]:
        """Apply add/update/delete changes and publish one consistent snapshot."""
        current = {self._row_key(item): item for item in corpus}
        added = removed = updated = 0
        with closing(self._connect()) as connection:
            existing_rows = connection.execute(
                "SELECT doc_id, content FROM lexical_documents"
            ).fetchall()
            existing = {str(row["doc_id"]): str(row["content"] or "") for row in existing_rows}
            current_keys = set(current)
            old_keys = set(existing)
            removed_keys = old_keys - current_keys
            added_keys = current_keys - old_keys
            updated_keys = {key for key in old_keys & current_keys
                            if existing[key] != str(current[key].page_content or "")}
            for key in removed_keys | updated_keys:
                connection.execute("DELETE FROM lexical_postings WHERE doc_id = ?", (key,))
                connection.execute("DELETE FROM lexical_documents WHERE doc_id = ?", (key,))
            for key in added_keys | updated_keys:
                item = current[key]
                row_key, source, content, metadata, tokens = self._row_payload(item)
                counts: dict[str, int] = {}
                for term in tokens:
                    counts[term] = counts.get(term, 0) + 1
                connection.execute(
                    "INSERT INTO lexical_documents(doc_id, source, content, metadata, length) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (row_key, source, content, metadata, len(tokens)),
                )
                connection.executemany(
                    "INSERT INTO lexical_postings(term, doc_id, term_frequency) VALUES (?, ?, ?)",
                    [(term, row_key, count) for term, count in counts.items()],
                )
            self._set_meta(connection, "schema", self.schema)
            self._set_meta(connection, "fingerprint", fingerprint)
            self._set_meta(connection, "documents", len(current))
            connection.commit()
        added, removed, updated = len(added_keys), len(removed_keys), len(updated_keys)
        self._fingerprint = fingerprint
        self._documents = len(current)
        self._ready = True
        return {"added": added, "removed": removed, "updated": updated}

    def query_hits(self, query: str, limit: int,
                   allowed_sources: Iterable[str] | None = None) -> list[LexicalHit]:
        terms = sorted(set(_tokens(query)))
        if not terms or not self.ready():
            return []
        limit = max(1, int(limit))
        with closing(self._connect()) as connection:
            total = max(1, int(self._meta(connection, "documents", "0") or 0))
            placeholders = ",".join("?" for _ in terms)
            df_rows = connection.execute(
                f"SELECT term, COUNT(*) AS df FROM lexical_postings "
                f"WHERE term IN ({placeholders}) GROUP BY term", terms
            ).fetchall()
            dfs = {str(row["term"]): int(row["df"]) for row in df_rows}
            avg_row = connection.execute(
                "SELECT AVG(length) AS avgdl FROM lexical_documents"
            ).fetchone()
            avgdl = float(avg_row["avgdl"] or 1.0)
            source_filter = tuple(dict.fromkeys(str(item) for item in (allowed_sources or ()) if str(item)))
            source_sql = ""
            source_params: list[str] = []
            if source_filter:
                source_sql = " AND d.source IN (" + ",".join("?" for _ in source_filter) + ")"
                source_params = list(source_filter)
            rows = connection.execute(
                f"SELECT p.term, p.doc_id, p.term_frequency, d.length "
                f"FROM lexical_postings p JOIN lexical_documents d ON d.doc_id = p.doc_id "
                f"WHERE p.term IN ({placeholders}){source_sql}", terms + source_params
            ).fetchall()
            scores: dict[str, float] = {}
            k1, b = 1.5, 0.75
            for row in rows:
                term = str(row["term"])
                df = max(1, dfs.get(term, 1))
                idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
                freq = int(row["term_frequency"])
                length = int(row["length"])
                norm = freq + k1 * (1.0 - b + b * length / avgdl)
                scores[str(row["doc_id"])] = scores.get(str(row["doc_id"]), 0.0) + (
                    idf * (freq * (k1 + 1.0) / norm)
                )
            ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
            if not ranked:
                return []
            ids = [item[0] for item in ranked]
            by_id = {
                str(row["doc_id"]): row
                for row in connection.execute(
                    f"SELECT doc_id, content, metadata FROM lexical_documents "
                    f"WHERE doc_id IN ({','.join('?' for _ in ids)})", ids
                ).fetchall()
            }
            hits: list[LexicalHit] = []
            for key, score in ranked:
                row = by_id.get(key)
                if row is None:
                    continue
                try:
                    metadata = dict(json.loads(str(row["metadata"] or "{}")))
                except (TypeError, ValueError):
                    metadata = {}
                metadata.setdefault("_id", key)
                hits.append(LexicalHit(key=key, score=float(score),
                                       document=Document(page_content=str(row["content"] or ""),
                                                         metadata=metadata)))
            return hits

    def scores(self, query: str) -> list[float]:
        """Compatibility view matching :class:`BM25Index.scores`.

        The production path uses ``query_hits`` directly.  This bounded
        adapter is retained for callers/tests that inspect a score vector;
        it reads only indexed metadata and postings, never rebuilds tokens.
        """
        if not self.ready():
            return []
        with closing(self._connect()) as connection:
            keys = [str(row[0]) for row in connection.execute(
                "SELECT doc_id FROM lexical_documents ORDER BY rowid").fetchall()]
        hits = self.query_hits(query, max(1, len(keys)))
        by_key = {item.key: float(item.score) for item in hits}
        return [by_key.get(key, 0.0) for key in keys]


def _lexical_backend() -> str:
    # SQLite postings are the production default: warm queries avoid loading
    # the full Chroma document snapshot and updates are incremental.  The JSON
    # snapshot remains an explicit compatibility fallback for constrained
    # deployments via DOCMIND_LEXICAL_BACKEND=snapshot.
    selected = os.getenv("DOCMIND_LEXICAL_BACKEND", "sqlite").strip().lower()
    return selected if selected in {"memory", "snapshot", "sqlite"} else "snapshot"


def get_lexical_index(collection: str, corpus: Sequence[Document] | None = None) -> tuple[Any, dict[str, Any]]:
    """Load a persisted BM25 snapshot, rebuilding only when Chroma changed.

    The collection snapshot is still read by the caller for permission/source
    shaping, but tokenization and document-frequency construction are cached in
    memory and on disk.  Mutation hooks invalidate the memory entry; the
    fingerprint then provides a safe cross-process stale-index check.
    """
    name = str(collection)
    backend = _lexical_backend()
    if backend == "sqlite":
        with _LEXICAL_LOCK:
            index = _LEXICAL_SQLITE.get(name)
            expected_path = _lexical_sqlite_path(name)
            # Tests, project switches and operator config can change the
            # index directory within one process. Never reuse a handle bound
            # to the previous collection path.
            if index is not None and os.path.normcase(str(index.path)) != os.path.normcase(expected_path):
                index = None
                _LEXICAL_SQLITE.pop(name, None)
            if index is None:
                index = SQLiteBM25Index(name, expected_path)
                _LEXICAL_SQLITE[name] = index
            stats = _LEXICAL_STATS.setdefault(name, {"hits": 0, "rebuilds": 0,
                                                      "disk_loads": 0, "invalidations": 0,
                                                      "last_delta": {}})
            if corpus is None and index.ready():
                stats["hits"] = int(stats.get("hits", 0)) + 1
                stats.update({"backend": "sqlite", "persistent": True,
                              "documents": index._documents, "fingerprint": index._fingerprint,
                              "ready": True})
                return index, dict(stats)
        if corpus is None:
            return index, dict(stats, backend="sqlite", persistent=True, ready=False,
                               documents=0, fingerprint="")
        fingerprint = _snapshot_fingerprint(corpus)
        with _LEXICAL_LOCK:
            if index.ready() and index._fingerprint == fingerprint:
                stats["hits"] = int(stats.get("hits", 0)) + 1
                stats.update({"backend": "sqlite", "persistent": True,
                              "documents": len(corpus), "fingerprint": fingerprint, "ready": True})
                return index, dict(stats)
            delta = index.sync(corpus, fingerprint)
            stats["rebuilds"] = int(stats.get("rebuilds", 0)) + 1
            stats.update({"backend": "sqlite", "persistent": True,
                          "documents": len(corpus), "fingerprint": fingerprint,
                          "last_delta": delta, "ready": True})
            return index, dict(stats)

    if corpus is None:
        corpus = []
    fingerprint = _snapshot_fingerprint(corpus)
    path = _lexical_path(name)
    persistent = os.getenv("DOCMIND_LEXICAL_PERSIST", "1").strip().lower() not in {"0", "false", "no"}
    with _LEXICAL_LOCK:
        stats = _LEXICAL_STATS.setdefault(name, {"hits": 0, "rebuilds": 0, "disk_loads": 0,
                                                  "invalidations": 0, "last_delta": {}})
        cached = _LEXICAL_MEMORY.get(name)
        if cached and cached[0] == fingerprint:
            stats["hits"] += 1
            stats.update({"backend": backend, "fingerprint": fingerprint,
                          "documents": len(corpus), "persistent": persistent})
            return cached[1], dict(stats)
        previous: list[dict[str, Any]] = []
        if persistent:
            disk = _read_lexical_file(path)
            if disk is not None:
                disk_fingerprint, previous = disk
                if disk_fingerprint == fingerprint:
                    index = BM25Index([str(row.get("content") or "") for row in previous])
                    stats["disk_loads"] += 1
                    stats.update({"backend": backend, "fingerprint": fingerprint, "documents": len(corpus),
                                  "persistent": True, "last_delta": {"added": 0, "removed": 0, "updated": 0}})
                    _LEXICAL_MEMORY[name] = (fingerprint, index, dict(stats))
                    return index, dict(stats)
        delta = _lexical_delta(previous, corpus)
        index = BM25Index([item.page_content for item in corpus])
        stats["rebuilds"] += 1
        stats.update({"backend": backend, "fingerprint": fingerprint, "documents": len(corpus),
                      "persistent": persistent, "last_delta": delta})
        if persistent:
            stats["persisted"] = _write_lexical_file(path, fingerprint, corpus)
        _LEXICAL_MEMORY[name] = (fingerprint, index, dict(stats))
        return index, dict(stats)


def invalidate_lexical_index(collection: str | None = None) -> None:
    """Invalidate memory cache after Chroma mutations; disk is fingerprint-checked."""
    with _LEXICAL_LOCK:
        if collection is None:
            names = sorted(set(_LEXICAL_MEMORY) | set(_LEXICAL_SQLITE))
            _LEXICAL_MEMORY.clear()
        else:
            names = [str(collection)]
            _LEXICAL_MEMORY.pop(str(collection), None)
            if str(collection) in _LEXICAL_SQLITE:
                _LEXICAL_SQLITE[str(collection)].invalidate()
        for name in names:
            if collection is None and name in _LEXICAL_SQLITE:
                _LEXICAL_SQLITE[name].invalidate()
            stats = _LEXICAL_STATS.setdefault(name, {})
            stats["invalidations"] = int(stats.get("invalidations", 0)) + 1


def lexical_status(collection: str | None = None) -> dict[str, Any]:
    with _LEXICAL_LOCK:
        if collection is not None:
            value = dict(_LEXICAL_STATS.get(str(collection), {}))
            value["memory_cached"] = str(collection) in _LEXICAL_MEMORY
            value["sqlite_cached"] = str(collection) in _LEXICAL_SQLITE
            return value
        return {name: dict(value) for name, value in _LEXICAL_STATS.items()}


class HeuristicReranker:
    """Dependency-free reranker used when no Cross-Encoder is installed."""

    name = "heuristic"

    def rerank(self, query: str, documents: Sequence[Document]) -> list[Document]:
        q_terms = set(_tokens(query))
        scored: list[tuple[float, float, int, Document]] = []
        for index, document in enumerate(documents):
            d_terms = set(_tokens(document.page_content))
            overlap = len(q_terms & d_terms) / max(1, len(q_terms))
            metadata = dict(document.metadata or {})
            base = float(metadata.get("hybrid_score") or 0.0)
            # Hybrid RRF is already the calibrated candidate order.  The
            # dependency-free reranker is only a deterministic tie-breaker;
            # giving lexical overlap a full-score weight can move a weak
            # document above a strong fused hit (observed on real Chroma
            # collections).  Preserve the fused order and use overlap as a
            # bounded secondary signal.
            scored.append((base, overlap, -index, document))
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        out: list[Document] = []
        for base, overlap, _index, document in scored:
            metadata = dict(document.metadata or {})
            metadata["rerank_score"] = round(base + overlap * 0.001, 6)
            out.append(Document(page_content=document.page_content, metadata=metadata))
        return out


class SentenceTransformerReranker:
    """Optional Cross-Encoder reranker, loaded only when explicitly enabled."""

    def __init__(self, model_name: str):
        self.model_name = str(model_name)
        self._model: Any | None = None
        self.error = ""
        self.name = "sentence_transformers"

    def _load(self) -> Any | None:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder(self.model_name)
        except Exception as exc:  # noqa: BLE001
            self.error = type(exc).__name__
            return None
        return self._model

    def rerank(self, query: str, documents: Sequence[Document]) -> list[Document]:
        model = self._load()
        if model is None or not documents:
            return list(documents)
        try:
            scores = model.predict([(query, item.page_content) for item in documents])
            ranked = sorted(zip(scores, documents), key=lambda item: float(item[0]), reverse=True)
        except Exception as exc:  # noqa: BLE001
            self.error = type(exc).__name__
            return list(documents)
        out: list[Document] = []
        for score, document in ranked:
            metadata = dict(document.metadata or {})
            metadata["rerank_score"] = float(score)
            out.append(Document(page_content=document.page_content, metadata=metadata))
        return out


def _make_reranker() -> Any | None:
    mode = os.getenv("DOCMIND_RERANKER", "heuristic").strip().lower()
    if mode in {"", "none", "off", "0", "false"}:
        return None
    if mode in {"cross_encoder", "sentence_transformers", "sentence-transformers"}:
        model = os.getenv("DOCMIND_RERANKER_MODEL", "BAAI/bge-reranker-base")
        return SentenceTransformerReranker(model)
    return HeuristicReranker()


def _metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep Chroma metadata JSON-safe and avoid leaking arbitrary objects."""
    return {str(key): item for key, item in (value or {}).items()
            if isinstance(key, (str, int, float, bool)) and
            isinstance(item, (str, int, float, bool, type(None)))}


class ChromaRetriever(BaseRetriever):
    """LangChain-compatible retriever backed by the existing Chroma wrapper."""

    collection_name: str = COLLECTION_NAME
    top_k: int = TOP_K
    source_allowlist: tuple[str, ...] = ()

    if PrivateAttr is not None:
        _embedding_client: Any = PrivateAttr(default=None)

    def __init__(self, *, collection: str = COLLECTION_NAME, top_k: int = TOP_K,
                 embedding_client: Any | None = None,
                 source_allowlist: Iterable[str] | None = None, **kwargs: Any):
        allowlist = tuple(dict.fromkeys(str(item) for item in (source_allowlist or ()) if str(item)))
        super().__init__(collection_name=str(collection), top_k=max(1, int(top_k)),
                         source_allowlist=allowlist, **kwargs)
        if PrivateAttr is not None:
            self._embedding_client = embedding_client
        else:  # pragma: no cover - fallback class has no pydantic internals
            self._embedding_client = embedding_client

    def _get_relevant_documents(self, query: str, *, run_manager: Any = None) -> list[Document]:
        text = str(query or "").strip()
        if not text:
            return []
        started = time.perf_counter()
        out: list[Document] = []
        with _retrieval_span(name="retrieval.dense", collection=self.collection_name,
                              mode="dense", query_chars=len(text),
                              inputs={"top_k": self.top_k}) as span:
            client = self._embedding_client or EmbeddingClient()
            embedding = client.embed([text])[0]
            where = {"source": {"$in": list(self.source_allowlist)}} if self.source_allowlist else None
            if where:
                result = vector_query(embedding, k=self.top_k, collection=self.collection_name, where=where)
            else:
                result = vector_query(embedding, k=self.top_k, collection=self.collection_name)
            documents = (result.get("documents") or [[]])[0]
            metadatas = (result.get("metadatas") or [[]])[0]
            distances = (result.get("distances") or [[]])[0]
            ids = (result.get("ids") or [[]])[0]
            for index, content in enumerate(documents):
                metadata = _metadata(metadatas[index] if index < len(metadatas) else {})
                if index < len(ids):
                    metadata.setdefault("_id", ids[index])
                if index < len(distances):
                    metadata.setdefault("distance", distances[index])
                out.append(Document(page_content=str(content or ""), metadata=metadata))
            span.update({"documents": len(out), "returned": len(out),
                         "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
        _record_retrieval_event(query=text, collection=self.collection_name, mode="dense",
                                documents=out, duration_ms=(time.perf_counter() - started) * 1000,
                                returned=len(out))
        return out


def _collection_snapshot(collection: str) -> list[Document]:
    """Read the bounded metadata needed for lexical retrieval from Chroma."""
    raw = get_collection(collection).get(include=["documents", "metadatas"])
    documents = list(raw.get("documents") or [])
    metadatas = list(raw.get("metadatas") or [])
    ids = list(raw.get("ids") or [])
    out: list[Document] = []
    for index, content in enumerate(documents):
        metadata = _metadata(metadatas[index] if index < len(metadatas) else {})
        if index < len(ids):
            metadata.setdefault("_id", ids[index])
        out.append(Document(page_content=str(content or ""), metadata=metadata))
    return out


class HybridRetriever(BaseRetriever):
    """BM25 + dense retrieval with reciprocal-rank fusion and reranking."""

    collection_name: str = COLLECTION_NAME
    top_k: int = TOP_K
    candidate_k: int = max(TOP_K * 4, 12)
    dense_weight: float = 1.0
    lexical_weight: float = 1.0
    rrf_k: int = 60
    source_allowlist: tuple[str, ...] = ()

    if PrivateAttr is not None:
        _embedding_client: Any = PrivateAttr(default=None)
        _reranker: Any = PrivateAttr(default=None)

    def __init__(self, *, collection: str = COLLECTION_NAME, top_k: int = TOP_K,
                 candidate_k: int | None = None, dense_weight: float | None = None,
                 lexical_weight: float | None = None, rrf_k: int = 60,
                 embedding_client: Any | None = None, reranker: Any | None = None,
                 source_allowlist: Iterable[str] | None = None,
                 **kwargs: Any):
        top = max(1, int(top_k))
        allowlist = tuple(dict.fromkeys(str(item) for item in (source_allowlist or ()) if str(item)))
        super().__init__(
            collection_name=str(collection), top_k=top,
            candidate_k=max(top, int(candidate_k or max(top * 4, 12))),
            dense_weight=float(dense_weight if dense_weight is not None else os.getenv("DOCMIND_DENSE_WEIGHT", "1")),
            lexical_weight=float(lexical_weight if lexical_weight is not None else os.getenv("DOCMIND_BM25_WEIGHT", "1")),
            rrf_k=max(1, int(rrf_k)), source_allowlist=allowlist, **kwargs)
        if PrivateAttr is not None:
            self._embedding_client = embedding_client
            self._reranker = reranker if reranker is not None else _make_reranker()
        else:  # pragma: no cover
            self._embedding_client = embedding_client
            self._reranker = reranker if reranker is not None else _make_reranker()

    def _get_relevant_documents(self, query: str, *, run_manager: Any = None) -> list[Document]:
        text = str(query or "").strip()
        if not text:
            return []
        started = time.perf_counter()
        selected: list[Document] = []
        candidate_count = 0
        mode = "hybrid_rerank" if self._reranker is not None and getattr(self._reranker, "name", "") == "sentence_transformers" else "hybrid"
        with _retrieval_span(name="retrieval.hybrid", collection=self.collection_name,
                              mode=mode, query_chars=len(text),
                              inputs={"top_k": self.top_k, "candidate_k": self.candidate_k,
                                      "dense_weight": self.dense_weight,
                                      "lexical_weight": self.lexical_weight,
                                      "reranker": getattr(self._reranker, "name", "none")}) as span:
            lexical_backend = _lexical_backend()
            corpus: list[Document] = []
            lexical_index: Any | None = None
            lexical_meta: dict[str, Any] = {}
            if lexical_backend == "sqlite":
                # SQLite can answer from postings without a Chroma-wide read.
                # A snapshot is needed only on first use or after an invalidation.
                lexical_index, lexical_meta = get_lexical_index(self.collection_name, None)
                if not lexical_meta.get("ready"):
                    corpus = _collection_snapshot(self.collection_name)
                    lexical_index, lexical_meta = get_lexical_index(self.collection_name, corpus)
            else:
                corpus = _collection_snapshot(self.collection_name)
                if self.source_allowlist:
                    allowed = set(self.source_allowlist)
                    corpus = [item for item in corpus
                              if str((item.metadata or {}).get("source") or "") in allowed]
            # dense 腿必须**独立可失败**。它最常见的失败原因是向量维度与集合不符
            #（Ollama 掉线 -> EmbeddingClient 降级成 256 维哈希词袋，而集合是
            # 1024 维）。此前这里没有 try/except，那个 ValueError 会掀掉整次
            # 检索——可下面还有一条完好的 BM25 词法腿根本用不上。
            # 名为 hybrid，就该真的是 hybrid：一腿断，另一腿还能出结果。
            # 失败原因记进 span，让「检索质量下降」在观测里看得见，
            # 而不是静默地少了一半候选。
            dense: list[Document] = []
            dense_error = ""
            try:
                if lexical_backend == "sqlite":
                    dense = ChromaRetriever(
                        collection=self.collection_name, top_k=self.candidate_k,
                        embedding_client=self._embedding_client,
                        source_allowlist=self.source_allowlist).invoke(text)
                elif not corpus:
                    span.update({"documents": 0, "returned": 0})
                else:
                    dense_k = min(self.candidate_k, len(corpus))
                    dense = ChromaRetriever(
                        collection=self.collection_name, top_k=dense_k,
                        embedding_client=self._embedding_client,
                        source_allowlist=self.source_allowlist).invoke(text)
            except Exception as exc:  # noqa: BLE001
                dense = []
                dense_error = "%s: %s" % (type(exc).__name__, exc)
                span.update({"dense_error": dense_error, "dense_degraded": True})
            if corpus or lexical_backend == "sqlite":
                dense_by_key = {_doc_key(item): item for item in dense}
                dense_rank = {_doc_key(item): index for index, item in enumerate(dense)}

                with _retrieval_span(name="retrieval.bm25", collection=self.collection_name,
                                     mode="bm25", query_chars=len(text),
                                     inputs={"candidate_k": self.candidate_k}) as bm_span:
                    if lexical_backend == "sqlite":
                        lexical_hits = lexical_index.query_hits(
                            text, self.candidate_k, allowed_sources=self.source_allowlist)
                        lexical_order = [hit.key for hit in lexical_hits]
                        lexical_rank = {hit.key: rank for rank, hit in enumerate(lexical_hits)}
                        bm25_by_key = {hit.key: hit.score for hit in lexical_hits}
                        lexical_docs = {hit.key: hit.document for hit in lexical_hits}
                    else:
                        lexical_index, lexical_meta = get_lexical_index(self.collection_name, corpus)
                        lexical_scores = lexical_index.scores(text)
                        lexical_order = sorted(range(len(corpus)), key=lambda index: lexical_scores[index], reverse=True)
                        lexical_rank = {_doc_key(corpus[index]): rank for rank, index in enumerate(lexical_order)
                                        if lexical_scores[index] > 0}
                        bm25_by_key = {_doc_key(item): lexical_scores[index]
                                       for index, item in enumerate(corpus)}
                        lexical_docs = {_doc_key(item): item for item in corpus}
                    bm_span.update({"documents": lexical_meta.get("documents", len(corpus)), "candidates": len(lexical_rank),
                                    "cache": "persistent" if lexical_meta.get("persistent") else "memory",
                                    "backend": lexical_meta.get("backend", lexical_backend),
                                    "added": (lexical_meta.get("last_delta") or {}).get("added", 0),
                                    "removed": (lexical_meta.get("last_delta") or {}).get("removed", 0),
                                    "updated": (lexical_meta.get("last_delta") or {}).get("updated", 0)})
                corpus_by_key = {_doc_key(item): item for item in corpus}
                corpus_by_key.update(lexical_docs)
                candidates: dict[str, Document] = {
                    key: corpus_by_key.get(key, document)
                    for key, document in dense_by_key.items()
                }
                if lexical_backend == "sqlite":
                    for key in lexical_order[:self.candidate_k]:
                        if key in lexical_docs:
                            candidates.setdefault(key, lexical_docs[key])
                else:
                    for index in lexical_order[:self.candidate_k]:
                        if lexical_scores[index] > 0:
                            candidates.setdefault(_doc_key(corpus[index]), corpus[index])
                candidate_count = len(candidates)

                ranked: list[tuple[float, Document]] = []
                for key, document in candidates.items():
                    score = 0.0
                    if key in dense_rank:
                        score += self.dense_weight / (self.rrf_k + dense_rank[key] + 1)
                    if key in lexical_rank:
                        score += self.lexical_weight / (self.rrf_k + lexical_rank[key] + 1)
                    metadata = dict(document.metadata or {})
                    metadata["dense_rank"] = dense_rank.get(key)
                    metadata["bm25_score"] = bm25_by_key.get(key, 0.0)
                    metadata["hybrid_score"] = score
                    metadata["lexical_index"] = "persistent" if lexical_meta.get("persistent") else "memory"
                    ranked.append((score, Document(page_content=document.page_content, metadata=metadata)))
                ranked.sort(key=lambda item: item[0], reverse=True)
                selected = [item for _score, item in ranked[:max(self.top_k, self.candidate_k)]]
                if self._reranker is not None:
                    with _retrieval_span(name="retrieval.reranker", collection=self.collection_name,
                                         mode=getattr(self._reranker, "name", "reranker"),
                                         query_chars=len(text),
                                         inputs={"candidates": len(selected)}) as rerank_span:
                        before = list(selected)
                        selected = self._reranker.rerank(text, selected)
                        rerank_span.update({"candidates": len(before), "returned": len(selected),
                                            "reranker": getattr(self._reranker, "name", "reranker"),
                                            "rerank_applied": selected != before})
                selected = selected[:self.top_k]
                span.update({"documents": len(corpus), "candidates": len(candidates),
                             "returned": len(selected), "reranker": getattr(self._reranker, "name", "none"),
                             "rerank_applied": bool(self._reranker),
                             "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                             "lexical_backend": lexical_backend})
        _record_retrieval_event(query=text, collection=self.collection_name, mode=mode,
                                documents=selected, duration_ms=(time.perf_counter() - started) * 1000,
                                candidates=candidate_count, returned=len(selected),
                                reranker=getattr(self._reranker, "name", "none"),
                                lexical_index=selected[0].metadata.get("lexical_index") if selected else "")
        return selected


def _doc_key(document: Document) -> str:
    metadata = dict(document.metadata or {})
    return str(metadata.get("_id") or (metadata.get("source", ""), document.page_content))


def get_retriever(*, collection: str = COLLECTION_NAME, top_k: int = TOP_K,
                  embedding_client: Any | None = None, mode: str | None = None,
                  **kwargs: Any) -> BaseRetriever:
    """Return the configured retriever through the standard component boundary."""
    selected = (mode or os.getenv("DOCMIND_RETRIEVAL_MODE", "hybrid")).strip().lower()
    if selected in {"dense", "vector", "chroma"}:
        return ChromaRetriever(collection=collection, top_k=top_k,
                               embedding_client=embedding_client, **kwargs)
    if selected in {"hybrid_rerank", "hybrid-cross-encoder", "hybrid_cross_encoder", "cross_encoder"}:
        # This explicit mode is an opt-in to the optional Cross-Encoder even
        # when the process-wide default disables reranking.  The reranker
        # still degrades to the original fused order when its dependency or
        # model cannot be loaded.
        if kwargs.get("reranker") is None:
            kwargs["reranker"] = SentenceTransformerReranker(
                os.getenv("DOCMIND_RERANKER_MODEL", "BAAI/bge-reranker-base"))
    return HybridRetriever(collection=collection, top_k=top_k,
                           embedding_client=embedding_client, **kwargs)


def retrieve_context(query: str, *, collections: Mapping[str, str],
                     top_k: int = TOP_K, max_chars: int = 2800,
                     embedding_client: Any | None = None,
                     mode: str | None = None,
                     source_allowlists: Mapping[str, Iterable[str]] | None = None) -> str:
    """Retrieve a bounded, source-labelled evidence block for an LLM prompt.

    The result is intentionally plain text so existing prompts and fallback
    providers can consume it.  ``Document`` remains the internal component
    contract, while this function is the narrow presentation boundary.
    """
    limit = max(400, int(max_chars))
    client = embedding_client or EmbeddingClient()
    seen: set[tuple[str, str]] = set()
    blocks: list[str] = []
    used = 0
    for label, collection in (collections or {}).items():
        if not collection:
            continue
        docs = []
        # Chroma/SQLite indexes can be cold or concurrently initialized when
        # a workflow starts. Retry only this bounded retrieval boundary; never
        # retry provider calls or hide a long-running failure in the planner.
        try:
            retry_count = max(0, min(3, int(os.getenv("DOCMIND_RETRIEVAL_RETRIES", "2"))))
        except (TypeError, ValueError):
            retry_count = 2
        for attempt in range(retry_count + 1):
            try:
                docs = get_retriever(collection=str(collection), top_k=top_k,
                                     embedding_client=client, mode=mode,
                                     source_allowlist=(source_allowlists or {}).get(label, ())).invoke(query)
                # A cold Chroma collection can transiently report an empty
                # result while its embedding/index state is being opened.
                # Treat that like a recoverable boundary result as well.
                if docs or attempt >= retry_count:
                    break
            except Exception as exc:
                _record_retrieval_event(query=query, collection=str(collection),
                                        mode="error", documents=[], duration_ms=0,
                                        attempt=attempt + 1, error=type(exc).__name__)
                if attempt < retry_count:
                    time.sleep(0.05 * (attempt + 1))
        # Retrieval is advisory for planning; a missing/empty collection still
        # falls back to the deterministic workflow path after bounded retries.
        for doc in docs:
            content = str(doc.page_content or "").strip()
            metadata = dict(doc.metadata or {})
            key = (str(metadata.get("source") or ""), content)
            if not content or key in seen:
                continue
            seen.add(key)
            source = str(metadata.get("source") or "?")
            location = ""
            if metadata.get("start_line"):
                location = ":L%s" % metadata.get("start_line")
                if metadata.get("end_line") and metadata.get("end_line") != metadata.get("start_line"):
                    location += "-L%s" % metadata.get("end_line")
            prefix = "[%s | %s%s]" % (str(label)[:40], source[:160], location)
            block = prefix + "\n" + content[:700]
            if used + len(block) + 5 > limit:
                remaining = limit - used - 5
                if remaining > 80:
                    blocks.append(block[:remaining].rstrip() + "…")
                return "\n---\n".join(blocks)
            blocks.append(block)
            used += len(block) + 5
    return "\n---\n".join(blocks)


def status(*, collection: str = COLLECTION_NAME, top_k: int = TOP_K) -> dict[str, Any]:
    """Return diagnostics without initializing an embedding provider."""
    try:
        import langchain_core  # noqa: F401
        available = True
    except Exception:
        available = False
    mode = os.getenv("DOCMIND_RETRIEVAL_MODE", "hybrid").strip().lower() or "hybrid"
    reranker = os.getenv("DOCMIND_RERANKER", "heuristic").strip().lower() or "heuristic"
    if mode in {"hybrid_rerank", "hybrid-cross-encoder", "hybrid_cross_encoder", "cross_encoder"}:
        reranker = "sentence_transformers"
    return {
        "backend": mode,
        "langchain_core": available,
        "collection": str(collection),
        "top_k": max(1, int(top_k)),
        "bm25": mode not in {"dense", "vector", "chroma"},
        "reranker": reranker,
        "reranker_model": os.getenv("DOCMIND_RERANKER_MODEL", ""),
        "lexical_persistence": os.getenv("DOCMIND_LEXICAL_PERSIST", "1").strip().lower()
        not in {"0", "false", "no"},
        "lexical_backend": _lexical_backend(),
        "lexical_index": lexical_status(collection),
        "recent_events": retrieval_events(20),
    }


__all__ = ["BM25Index", "SQLiteBM25Index", "ChromaRetriever", "Document", "HeuristicReranker",
           "HybridRetriever", "RetrieverStatus", "SentenceTransformerReranker",
           "get_retriever", "get_lexical_index", "invalidate_lexical_index",
           "lexical_status", "retrieval_events", "retrieve_context", "status"]
