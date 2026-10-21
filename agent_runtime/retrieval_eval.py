"""Offline retrieval evaluation for dense, hybrid and reranked retrievers."""
from __future__ import annotations

import os
from pathlib import PurePath
from typing import Any, Iterable, Mapping, Sequence

from config import COLLECTION_NAME, TOP_K
from .retrieval import BM25Index, Document, get_retriever


DEFAULT_KS = (1, 3, 5)


def load_retrieval_dataset(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load a labelled lexical benchmark with explicit source/id contracts."""
    import json
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise ValueError("retrieval dataset must be an object")
    documents = value.get("documents")
    cases = value.get("cases")
    if not isinstance(documents, list) or not documents or not isinstance(cases, list) or not cases:
        raise ValueError("retrieval dataset requires non-empty documents and cases")
    normalized_docs = []
    for row in documents:
        if not isinstance(row, Mapping) or not str(row.get("id") or "").strip() \
                or not str(row.get("content") or "").strip():
            raise ValueError("each retrieval document requires id and content")
        normalized_docs.append({"id": str(row["id"]), "source": str(row.get("source") or ""),
                                "content": str(row["content"])})
    normalized_cases = []
    for row in cases:
        if not isinstance(row, Mapping) or not str(row.get("query") or "").strip():
            raise ValueError("each retrieval case requires query")
        if not (row.get("relevant_ids") or row.get("relevant_sources") or row.get("relevant_terms")):
            raise ValueError("each retrieval case requires a relevance label")
        normalized_cases.append(dict(row))
    return {"version": int(value.get("version") or 1), "documents": normalized_docs,
            "cases": normalized_cases, "ks": list(value.get("ks") or DEFAULT_KS)}


def load_retrieval_cases(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load labels for a live Chroma collection without copying its corpus.

    Live datasets deliberately contain only queries and relevance labels.  The
    indexed documents remain in the user's project-scoped Chroma collection.
    """
    import json
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping) or not isinstance(value.get("cases"), list) \
            or not value["cases"]:
        raise ValueError("live retrieval dataset requires non-empty cases")
    cases = []
    for row in value["cases"]:
        if not isinstance(row, Mapping) or not str(row.get("query") or "").strip():
            raise ValueError("each live retrieval case requires query")
        if not (row.get("relevant_ids") or row.get("relevant_sources") or row.get("relevant_terms")):
            raise ValueError("each live retrieval case requires a relevance label")
        cases.append(dict(row))
    return {"version": int(value.get("version") or 1), "cases": cases,
            "ks": list(value.get("ks") or DEFAULT_KS),
            "collection": str(value.get("collection") or "")}


def evaluate_bm25_corpus(documents: Sequence[Mapping[str, Any]], cases: Iterable[Mapping[str, Any]],
                         ks: Sequence[int] = DEFAULT_KS) -> dict[str, Any]:
    """Evaluate the persisted lexical component without embeddings/network."""
    rows = [dict(item) for item in documents if isinstance(item, Mapping)]
    docs = [Document(page_content=str(item.get("content") or ""), metadata={
        "_id": str(item.get("id") or ""), "source": str(item.get("source") or "")})
            for item in rows]
    index = BM25Index([doc.page_content for doc in docs])

    class _BM25Retriever:
        def invoke(self, query: str):
            scores = index.scores(query)
            order = sorted(range(len(docs)), key=lambda row: scores[row], reverse=True)
            return [docs[row] for row in order]

    return evaluate_retriever(_BM25Retriever(), cases, ks=ks)


def retrieval_regression_gate(current: Mapping[str, Any], baseline: Mapping[str, Any] | None = None,
                              *, metric: str = "mrr", minimum: float = 0.0) -> dict[str, Any]:
    """Fail CI when the labelled retrieval benchmark regresses."""
    current_metrics = dict(current.get("metrics") or {})
    baseline_metrics = dict((baseline or {}).get("metrics") or {})
    current_value = float(current_metrics.get(metric) or 0.0)
    baseline_value = float(baseline_metrics.get(metric)) if metric in baseline_metrics else None
    return {
        "metric": metric, "current": round(current_value, 4),
        "baseline": round(baseline_value, 4) if baseline_value is not None else None,
        "minimum": float(minimum),
        "regressed": current_value < float(minimum) or (
            baseline_value is not None and current_value < baseline_value),
    }


def _source_name(value: Any) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").split("/")[-1].lower()


def _matches(document: Document, case: Mapping[str, Any]) -> bool:
    metadata = dict(document.metadata or {})
    doc_id = str(metadata.get("_id") or "")
    source = _source_name(metadata.get("source"))
    relevant_ids = {str(item) for item in (case.get("relevant_ids") or [])}
    if relevant_ids and doc_id in relevant_ids:
        return True
    relevant_sources = {_source_name(item) for item in (case.get("relevant_sources") or [])}
    if relevant_sources and source in relevant_sources:
        return True
    terms = [str(item).strip().lower() for item in (case.get("relevant_terms") or []) if str(item).strip()]
    content = str(document.page_content or "").lower()
    return bool(terms and all(term in content for term in terms))


def evaluate_retriever(retriever: Any, cases: Iterable[Mapping[str, Any]],
                       ks: Sequence[int] = DEFAULT_KS) -> dict[str, Any]:
    """Evaluate a retriever against source/id/term-labelled cases."""
    normalized_ks = tuple(sorted({max(1, int(k)) for k in ks})) or DEFAULT_KS
    rows: list[dict[str, Any]] = []
    for raw_case in cases:
        case = dict(raw_case or {})
        query = str(case.get("query") or "").strip()
        if not query:
            continue
        try:
            documents = list(retriever.invoke(query))
            error = ""
        except Exception as exc:  # noqa: BLE001
            documents = []
            error = type(exc).__name__
        ranks = [index for index, document in enumerate(documents)
                 if _matches(document, case)]
        first_rank = min(ranks) if ranks else None
        row: dict[str, Any] = {
            "id": str(case.get("id") or query[:80]),
            "query": query,
            "retrieved": len(documents),
            "first_relevant_rank": (first_rank + 1) if first_rank is not None else None,
            "mrr": (1.0 / (first_rank + 1)) if first_rank is not None else 0.0,
            "error": error,
            "hits": {},
        }
        for k in normalized_ks:
            row["hits"][str(k)] = bool(any(rank < k for rank in ranks))
        rows.append(row)
    total = len(rows)
    metrics: dict[str, Any] = {
        "queries": total,
        "mrr": round(sum(row["mrr"] for row in rows) / total, 4) if total else 0.0,
        "errors": sum(1 for row in rows if row["error"]),
    }
    for k in normalized_ks:
        metrics[f"recall_at_{k}"] = round(
            sum(1 for row in rows if row["hits"].get(str(k))) / total, 4) if total else 0.0
    return {"metrics": metrics, "ks": list(normalized_ks), "items": rows}


def evaluate_modes(cases: Iterable[Mapping[str, Any]], *, collection: str = COLLECTION_NAME,
                   top_k: int = TOP_K, modes: Sequence[str] = ("dense", "hybrid"),
                   reranker: Any | None = None,
                   ks: Sequence[int] = DEFAULT_KS) -> dict[str, Any]:
    """Run the same labelled cases through multiple retriever modes."""
    rows: dict[str, Any] = {}
    case_list = [dict(item or {}) for item in cases]
    for mode in modes:
        selected = str(mode or "hybrid").strip().lower()
        kwargs = {"mode": selected}
        if reranker is not None:
            kwargs["reranker"] = reranker
        retriever = get_retriever(collection=collection, top_k=max(1, int(top_k)), **kwargs)
        rows[selected] = evaluate_retriever(retriever, case_list, ks=ks)
    return {"collection": collection, "modes": rows}


def compare_reports(reports: Mapping[str, Mapping[str, Any]], metric: str = "mrr") -> dict[str, Any]:
    """Return a stable ranking of retriever reports by one metric."""
    ranking = []
    for name, report in (reports or {}).items():
        value = float((report.get("metrics") or {}).get(metric) or 0.0)
        ranking.append({"mode": str(name), "metric": metric, "value": round(value, 4)})
    ranking.sort(key=lambda item: (item["value"], item["mode"]), reverse=True)
    return {"metric": metric, "ranking": ranking, "best": ranking[0]["mode"] if ranking else ""}


def live_regression_gate(reports: Mapping[str, Mapping[str, Any]],
                         baseline: Mapping[str, Any] | None = None,
                         *, metric: str = "mrr", minimum: float = 0.0) -> dict[str, Any]:
    """Apply a minimum and per-mode baseline to a live collection report."""
    baseline_modes = (baseline or {}).get("modes") or {}
    rows = []
    regressed = False
    for mode, report in (reports or {}).items():
        current = float((report.get("metrics") or {}).get(metric) or 0.0)
        old = None
        if isinstance(baseline_modes.get(mode), Mapping):
            old = float((baseline_modes[mode].get("metrics") or {}).get(metric) or 0.0)
        failed = current < float(minimum) or (old is not None and current < old)
        regressed = regressed or failed
        rows.append({"mode": mode, "metric": metric, "current": round(current, 4),
                     "baseline": round(old, 4) if old is not None else None,
                     "minimum": float(minimum), "regressed": failed})
    return {"metric": metric, "minimum": float(minimum), "regressed": regressed,
            "modes": rows}


def default_cases() -> list[dict[str, Any]]:
    """Return a small schema example; projects should provide real labels."""
    return [
        {"id": "example-code", "query": "movement controller",
         "relevant_sources": ["player.py"]},
        {"id": "example-doc", "query": "embedding configuration",
         "relevant_sources": ["README.md"]},
    ]


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Agent Workbench retrieval regression gate")
    parser.add_argument("--dataset", default="")
    parser.add_argument("--live-cases", default="",
                        help="evaluate labelled cases against a live Chroma collection")
    parser.add_argument("--collection", default=COLLECTION_NAME)
    parser.add_argument("--modes", default="dense,hybrid,hybrid_rerank")
    parser.add_argument("--baseline", default="")
    parser.add_argument("--metric", default="mrr")
    parser.add_argument("--minimum", type=float, default=0.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.live_cases:
            dataset = load_retrieval_cases(args.live_cases)
            report = evaluate_modes(dataset["cases"], collection=args.collection,
                                    top_k=max(dataset["ks"]),
                                    modes=[item.strip() for item in args.modes.split(",") if item.strip()],
                                    ks=dataset["ks"])
        elif args.dataset:
            dataset = load_retrieval_dataset(args.dataset)
            report = evaluate_bm25_corpus(dataset["documents"], dataset["cases"], dataset["ks"])
        else:
            raise ValueError("--dataset or --live-cases is required")
        baseline = None
        if args.baseline:
            with open(args.baseline, "r", encoding="utf-8") as handle:
                baseline = json.load(handle)
        gate = (live_regression_gate(report.get("modes") or {}, baseline,
                                     metric=args.metric, minimum=args.minimum)
                if args.live_cases else
                retrieval_regression_gate(report, baseline, metric=args.metric, minimum=args.minimum))
        output = {"report": report, "gate": gate}
        print(json.dumps(output, ensure_ascii=False, indent=2) if args.as_json else output)
        return 1 if gate["regressed"] else 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print("retrieval regression gate failed: %s" % exc)
        return 2


__all__ = ["compare_reports", "default_cases", "evaluate_bm25_corpus", "evaluate_modes",
           "evaluate_retriever", "load_retrieval_cases", "load_retrieval_dataset", "live_regression_gate",
           "main", "retrieval_regression_gate"]


if __name__ == "__main__":
    raise SystemExit(main())
